"""Datasets: one event type each, written as an ODCS v3 data contract (the Linux Foundation's Open
Data Contract Standard, https://bitol-io.github.io/open-data-contract-standard/) with Valxel's
extensions in its customProperties. The design "Datasets, contracts and reporting" says why.

  architecture  The architecture agent writes a contract for every event under the HLD's
                '## Datasets' section (one fenced yaml block each); people approve them at G4.
  every gate    The evaluator checks them (this module's `problems` and `breaking`), and the
                events, knowledge sources and consumers the HLD names against them.
  build         Coders copy the generated JSON Schema into each repo that publishes or reads the
                dataset (contracts/<id>.json plus contracts/lock.yaml); producers validate before
                publishing and consumers on read. At run time nothing reads GitHub.
  release       The contracts go to the product's governance repo, datasets/<id>.yaml, with the
                feature's decisions (knowledge.writeback).

Valxel's profile of ODCS, on top of the standard's own schema:
  - apiVersion v3.x, kind DataContract, id '<product>.<name>', a semantic `version`.
  - `schema` holds one object: physicalType topic, physicalName the topic ('<prefix>.<entity>.v<n>').
  - Every property has a logicalType and a `classification`: none, pii, financial, phi or secret.
    At least one property is the record's key (primaryKey: true).
  - customProperties: owner (the one repo that publishes it), events (the CloudEvent types on the
    topic), compacted, retention, tenant (the property that says whose record it is, if any) and
    consumers (repo -> the properties it reads).
  - No `servers`: ODCS requires a host for Kafka, and addresses differ per environment. The tool
    resolves the product's Kafka itself.

This file is also governance repos' CI check (.github/scripts/datasets.py, a managed file): it uses
only the standard library and PyYAML, plus jsonschema and the ODCS schema file when they're there.

    python datasets.py check datasets/ [--base origin/main]
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

CLASSES = ("none", "pii", "financial", "phi", "secret")  # loosest to strictest
RANK = {c: i for i, c in enumerate(CLASSES)}
EXTENSIONS = ("owner", "events", "compacted", "retention", "tenant", "consumers")
ID = re.compile(r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9_.-]*$")
TOPIC = re.compile(r"^[a-z][a-z0-9_.-]*\.v(\d+)$")
EVENT = re.compile(r"^valxel\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.v\d+$")
REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
FIELD = re.compile(r"^[a-z][a-z0-9_]*$")
RETENTION = re.compile(r"^\d+[dmy]$")
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
ODCS_SCHEMA = "odcs-json-schema-v3.2.0.json"

# ODCS logical types -> JSON Schema for the CloudEvent's `data`.
TYPES: dict[str, dict] = {
    "string": {"type": "string"},
    "date": {"type": "string", "format": "date"},
    "timestamp": {"type": "string", "format": "date-time"},
    "time": {"type": "string", "format": "time"},
    "number": {"type": "number"},
    "integer": {"type": "integer"},
    "boolean": {"type": "boolean"},
    "object": {"type": "object"},
    "map": {"type": "object"},
    "array": {"type": "array"},
    "vector": {"type": "array", "items": {"type": "number"}},
}


# --- reading a contract -------------------------------------------------------------------------

def load(text: str) -> dict:
    """A contract from YAML; {} when it isn't a mapping."""
    data = yaml.safe_load(text or "")
    return data if isinstance(data, dict) else {}


def extensions(c: dict) -> dict[str, Any]:
    """Valxel's customProperties as a dict: owner, events, compacted, retention, tenant, consumers."""
    out: dict[str, Any] = {}
    for p in c.get("customProperties") or []:
        if isinstance(p, dict) and p.get("property") in EXTENSIONS:
            out[p["property"]] = p.get("value")
    return out


def topic_object(c: dict) -> dict:
    objs = [o for o in c.get("schema") or [] if isinstance(o, dict)]
    return objs[0] if len(objs) == 1 else {}


def topic(c: dict) -> str:
    return str(topic_object(c).get("physicalName") or "")


def properties(c: dict) -> list[dict]:
    return [p for p in topic_object(c).get("properties") or [] if isinstance(p, dict)]


def fields(c: dict) -> dict[str, dict]:
    return {str(p.get("name")): p for p in properties(c)}


def consumers(c: dict) -> dict[str, list[str]]:
    raw = extensions(c).get("consumers") or {}
    return {str(k): [str(f) for f in (v or [])] for k, v in raw.items()} if isinstance(raw, dict) else {}


def events(c: dict) -> list[str]:
    raw = extensions(c).get("events") or []
    return [str(e) for e in raw] if isinstance(raw, list) else []


# --- checks -------------------------------------------------------------------------------------

def _odcs_errors(c: dict, schema_path: Path | None) -> list[str]:
    """The standard's own JSON Schema, when jsonschema and the schema file are available."""
    if schema_path is None or not schema_path.exists():
        return []
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - CI installs it; the tool has it
        return []
    validator = jsonschema.Draft201909Validator(json.loads(schema_path.read_text()))
    out = []
    for e in sorted(validator.iter_errors(c), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in e.absolute_path) or "(top)"
        out.append(f"ODCS: {where}: {e.message[:200]}")
    return out


def problems(c: dict, schema_path: Path | None = None) -> list[str]:
    """What's wrong with one contract under Valxel's profile of ODCS (and the standard's own schema
    when `schema_path` is given). The contract's id prefixes each message."""
    cid = str(c.get("id") or "(no id)")
    out: list[str] = []

    def bad(msg: str) -> None:
        out.append(f"{cid}: {msg}")

    if not str(c.get("apiVersion", "")).startswith("v3."):
        bad("apiVersion must be an ODCS v3 version, e.g. v3.2.0")
    if c.get("kind") != "DataContract":
        bad("kind must be DataContract")
    if not ID.match(cid):
        bad("id must be '<product>.<name>' in lower case, e.g. emr.notes")
    if not SEMVER.match(str(c.get("version", ""))):
        bad("version must be semantic, e.g. 1.0.0 (minor for a compatible change)")
    if c.get("servers"):
        bad("leave out servers: Kafka addresses differ per environment, and the tool resolves them")
    obj = topic_object(c)
    if not obj:
        bad("schema must hold exactly one object: the topic")
    else:
        if obj.get("physicalType") != "topic":
            bad("the schema object's physicalType must be topic")
        if not TOPIC.match(str(obj.get("physicalName") or "")):
            bad("the schema object's physicalName must be the topic, '<prefix>.<entity>.v<n>'")
    props = properties(c)
    if obj and not props:
        bad("the topic has no properties")
    names = [str(p.get("name")) for p in props]
    for n in sorted({n for n in names if names.count(n) > 1}):
        bad(f"property {n} appears more than once")
    for p in props:
        n = str(p.get("name"))
        if not FIELD.match(n):
            bad(f"property {n}: names are snake_case")
        if p.get("logicalType") not in TYPES:
            bad(f"property {n}: logicalType must be one of {', '.join(TYPES)}")
        if p.get("classification") not in CLASSES:
            bad(f"property {n}: classification must be one of {', '.join(CLASSES)}")
    if props and not any(p.get("primaryKey") for p in props):
        bad("no property is the record's key (primaryKey: true): events are keyed by it")

    ext = extensions(c)
    for k in ("owner", "events", "retention", "consumers"):
        if k not in ext:
            bad(f"customProperties needs {k}")
    if "owner" in ext and not REPO.match(str(ext["owner"])):
        bad("owner must be the one repo that publishes it, '<org>/<repo>'")
    ev = events(c)
    if "events" in ext and not ev:
        bad("events must list the CloudEvent types on the topic")
    for e in ev:
        if not EVENT.match(e):
            bad(f"event {e}: types are 'valxel.<record>.<what happened>.v<n>'")
    if "compacted" in ext and not isinstance(ext["compacted"], bool):
        bad("compacted must be true or false")
    if "retention" in ext and not RETENTION.match(str(ext["retention"])):
        bad("retention must be a number of days, months or years, e.g. 90d or 7y")
    tenant = ext.get("tenant")
    if tenant is not None and str(tenant) not in names:
        bad(f"tenant {tenant} isn't a property")
    raw = ext.get("consumers")
    if raw is not None and not isinstance(raw, dict):
        bad("consumers must map each repo to the properties it reads")
    for repo, wanted in consumers(c).items():
        if not REPO.match(repo):
            bad(f"consumer {repo} must be '<org>/<repo>'")
        missing = [f for f in wanted if f not in names]
        if missing:
            bad(f"consumer {repo} reads {', '.join(missing)}, which the contract doesn't have")
        if repo == ext.get("owner"):
            bad(f"{repo} owns the dataset; it isn't also a consumer")
    return out + [f"{cid}: {e}" for e in _odcs_errors(c, schema_path)]


def _version(c: dict) -> tuple[int, int, int]:
    m = SEMVER.match(str(c.get("version", "")))
    return (int(m[1]), int(m[2]), int(m[3])) if m else (0, 0, 0)


def _enum(p: dict) -> list:
    return [e.get("value") if isinstance(e, dict) else e for e in p.get("enum") or []]


def breaking(old: dict, new: dict) -> list[str]:
    """What a new version of a contract (same id) does that consumers on the old one can't take.
    Within an event version only compatible changes are allowed: adding an optional property,
    adding an enum value, tightening a class. Anything else is a new event type and topic, so a
    new contract."""
    cid = str(new.get("id") or old.get("id"))
    out: list[str] = []
    of, nf = fields(old), fields(new)
    if topic(old) != topic(new):
        out.append(f"{cid}: the topic changed from {topic(old)} to {topic(new)}; a new topic is a new contract")
    for name, p in of.items():
        q = nf.get(name)
        if q is None:
            out.append(f"{cid}: property {name} was removed")
            continue
        if p.get("logicalType") != q.get("logicalType"):
            out.append(f"{cid}: property {name} changed type from {p.get('logicalType')} to {q.get('logicalType')}")
        if q.get("required") and not p.get("required"):
            out.append(f"{cid}: property {name} became required")
        if RANK.get(q.get("classification"), 0) < RANK.get(p.get("classification"), 0):
            out.append(f"{cid}: property {name}'s class was loosened from {p.get('classification')} to {q.get('classification')}")
        if bool(p.get("primaryKey")) != bool(q.get("primaryKey")):
            out.append(f"{cid}: property {name} changed whether it is the key")
        gone = [v for v in _enum(p) if _enum(q) and v not in _enum(q)]
        if gone:
            out.append(f"{cid}: property {name} lost enum values {', '.join(map(str, gone))}")
    for name, q in nf.items():
        if name not in of and q.get("required"):
            out.append(f"{cid}: new property {name} is required; add it as optional")
    removed = [e for e in events(old) if e not in events(new)]
    if removed:
        out.append(f"{cid}: event types {', '.join(removed)} were removed")
    if extensions(old).get("compacted") and not extensions(new).get("compacted"):
        out.append(f"{cid}: the topic is no longer compacted; rebuilds from it would miss records")
    if out:
        return out
    if json_schema(old) != json_schema(new) and _version(new) <= _version(old):
        out.append(f"{cid}: the schema changed, so raise its version above {old.get('version')}")
    return out


# --- what components use ------------------------------------------------------------------------

def json_schema(c: dict) -> dict:
    """JSON Schema (2020-12) for the CloudEvent `data` of this dataset. Producers validate against
    it as it is: a property the contract doesn't have is refused, so nothing unclassified is
    published. Consumers ignore properties newer than their copy, then validate."""
    props: dict[str, dict] = {}
    for p in properties(c):
        s = dict(TYPES.get(str(p.get("logicalType")), {}))
        opts = p.get("logicalTypeOptions") or {}
        if p.get("logicalType") == "string":
            for k in ("minLength", "maxLength", "pattern", "format"):
                if k in opts:
                    s[k] = opts[k]
        if _enum(p):
            s["enum"] = _enum(p)
        s["x-classification"] = p.get("classification")
        if p.get("description"):
            s["description"] = str(p["description"])
        props[str(p.get("name"))] = s
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"valxel:dataset/{c.get('id')}/{c.get('version')}",
        "title": str(c.get("name") or c.get("id")),
        "type": "object",
        "properties": props,
        "required": [str(p.get("name")) for p in properties(c) if p.get("required") or p.get("primaryKey")],
        "additionalProperties": False,
    }


def fingerprint(c: dict) -> str:
    """Stable hash of the generated schema: a repo's contracts/lock.yaml records it, and the drift
    check compares it with the registry's."""
    return hashlib.sha256(json.dumps(json_schema(c), sort_keys=True).encode()).hexdigest()[:16]


def lock(contracts: list[dict]) -> str:
    """contracts/lock.yaml for a repo: each dataset it publishes or reads, its version and hash."""
    rows = {str(c["id"]): {"version": str(c.get("version")), "sha": fingerprint(c), "topic": topic(c)}
            for c in sorted(contracts, key=lambda c: str(c.get("id")))}
    return ("# Written by the Valxel SDLC from the product's data contracts; don't edit by hand.\n"
            + yaml.safe_dump(rows, sort_keys=True))


def for_repo(contracts: list[dict], repo: str) -> list[dict]:
    """The contracts a repo publishes (owner) or reads (consumers)."""
    return [c for c in contracts if extensions(c).get("owner") == repo or repo in consumers(c)]


def repo_files(contracts: list[dict], repo: str) -> dict[str, str]:
    """What the tool writes into a repo that publishes or reads datasets: contracts/<id>.json (the
    schema it validates against) and contracts/lock.yaml. Empty when the repo uses none."""
    mine = for_repo(contracts, repo)
    if not mine:
        return {}
    files = {f"contracts/{c['id']}.json": json.dumps(json_schema(c), indent=2, sort_keys=True) + "\n" for c in mine}
    files["contracts/lock.yaml"] = lock(mine)
    return files


def drift(contracts: list[dict], repo: str, lock_text: str | None) -> list[str]:
    """How a repo's contracts/lock.yaml differs from the approved contracts it uses (G5)."""
    want = {str(c["id"]): fingerprint(c) for c in for_repo(contracts, repo)}
    if not want:
        return []
    if not lock_text:
        return [f"{repo} has no contracts/lock.yaml; the tool writes it with the schemas it validates against"]
    have = {k: (v or {}).get("sha") for k, v in (yaml.safe_load(lock_text) or {}).items()}
    out = [f"{repo}: contracts/{cid}.json is missing or stale (approved {want[cid]}, the repo has {have.get(cid) or 'none'})"
           for cid in sorted(want) if have.get(cid) != want[cid]]
    return out + [f"{repo}: contracts/lock.yaml lists {cid}, which no approved contract gives it"
                  for cid in sorted(set(have) - set(want))]


def instruction(contracts: list[dict], repo: str) -> str:
    """The coder's rules for the datasets this repo publishes or reads."""
    mine = for_repo(contracts, repo)
    if not mine:
        return ""
    out = []
    for c in mine:
        cid, schema = str(c["id"]), f"contracts/{c['id']}.json"
        if extensions(c).get("owner") == repo:
            out.append(f" This repo publishes {cid} on {topic(c)} ({', '.join(events(c))}): validate each event's"
                       f" data against {schema} before publishing (the template's event contracts do it; an event that"
                       f" fails is a bug: refuse the request and publish nothing) and set the CloudEvent's dataschema"
                       f" to the schema's $id. Publish only the contract's properties.")
        else:
            out.append(f" This repo reads {cid} from {topic(c)} and may use only {', '.join(consumers(c).get(repo, []))}:"
                       f" drop properties your copy of {schema} doesn't have (a newer compatible producer), validate the"
                       " rest, and send an event that fails to the dead-letter topic naming the field, never its value.")
    return ("".join(out) + " The tool writes contracts/ from the approved contracts; never edit it, and change a"
            " contract only through the design.")


# --- the HLD ------------------------------------------------------------------------------------

_BLOCK = re.compile(r"```ya?ml\s*\n(.*?)```", re.S)


def parse_hld(text: str) -> tuple[list[dict], list[str]]:
    """The contracts in the HLD's '## Datasets' section (one fenced yaml block each), and the
    blocks that didn't parse."""
    m = re.search(r"^## Datasets\s*$(.*?)(?=^## |\Z)", text or "", re.M | re.S)
    if not m:
        return [], []
    out, errors = [], []
    for i, block in enumerate(_BLOCK.findall(m.group(1)), 1):
        try:
            c = load(block)
        except yaml.YAMLError as exc:
            errors.append(f"Datasets block {i} isn't valid YAML: {str(exc).splitlines()[0]}")
            continue
        if c:
            out.append(c)
        else:
            errors.append(f"Datasets block {i} isn't a data contract")
    return out, errors


# --- the governance repo's CI check -------------------------------------------------------------

def _base_text(path: Path, base: str) -> str | None:
    try:
        return subprocess.run(["git", "show", f"{base}:{path.as_posix()}"], capture_output=True,  # noqa: S603,S607
                              text=True, check=True).stdout
    except subprocess.CalledProcessError:
        return None


def check_dir(directory: Path, base: str = "", schema_path: Path | None = None) -> list[str]:
    """Every contract under `directory` is valid; with `base`, each changed one is compatible with
    its version on that ref. Ids are unique, and so are topics."""
    out: list[str] = []
    seen_ids: dict[str, str] = {}
    seen_topics: dict[str, str] = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            c = load(path.read_text())
        except yaml.YAMLError as exc:
            out.append(f"{path.name}: not valid YAML: {str(exc).splitlines()[0]}")
            continue
        out += [f"{path.name}: {p}" for p in problems(c, schema_path)]
        cid = str(c.get("id"))
        if path.stem != cid:
            out.append(f"{path.name}: the file name must be the contract's id ({cid}.yaml)")
        if cid in seen_ids:
            out.append(f"{path.name}: id {cid} is also in {seen_ids[cid]}")
        seen_ids[cid] = path.name
        t = topic(c)
        if t and t in seen_topics:
            out.append(f"{path.name}: topic {t} is also in {seen_topics[t]}")
        seen_topics[t] = path.name
        if base:
            old = _base_text(path, base)
            if old:
                out += [f"{path.name}: {b}" for b in breaking(load(old), c)]
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "check":
        print("usage: datasets.py check <dir> [--base <git ref>]", file=sys.stderr)
        return 2
    directory = Path(args[1] if len(args) > 1 and not args[1].startswith("--") else "datasets")
    base = args[args.index("--base") + 1] if "--base" in args else ""
    schema = Path(__file__).with_name(ODCS_SCHEMA)
    if not directory.exists():
        print(f"No {directory}/: nothing to check.")
        return 0
    found = check_dir(directory, base, schema if schema.exists() else None)
    for f in found:
        print(f"::error::{f}")
    print(f"{len(list(directory.glob('*.yaml')))} contracts checked, {len(found)} problems.")
    return 1 if found else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
