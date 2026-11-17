"""Document intake (role `document-intake`, valxel-platform/template-document-intake).

People upload documents; the component checks them (size, format by content, malware), reads the
fields the product needs through the LLM gateway, asks a person to review what it isn't sure of
and publishes the fields as a data contract. What it takes is approved at G4 and held to on the
branch:

  the HLD          '## Documents' names, per intake repo, each document type: formats, size limit,
                   retention, the dataset its fields are published under, and the fields (`*` for
                   required): '- acme-emr/emr-intake: lab-report; formats pdf, png; max 10 MB;
                   keep 2555 days; dataset lab-report-extracted; fields patient_ref*, collected_on*,
                   test_code'
  Resources        two S3 buckets it reads, writes and deletes: quarantine (uploads land there;
                   expire_days of at most 7 so abandoned uploads go) and clean (checked files)
  Endpoints        project: people reach it through the product's BFF, never directly
  intake.yaml      the repo's own file, which the coder writes from the lines; the build fails when
                   it takes more (another type or format, a larger size, a shorter retention, another
                   dataset) than the design approved (config_findings)
"""

from __future__ import annotations

import re

import yaml

ROLE = "document-intake"
PATH = "intake.yaml"
FORMATS = ("pdf", "png", "jpeg", "tiff")
_LINE = re.compile(r"^- ([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)\s*:\s*(.+?)\s*$", re.M)
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,62}\*?$")


def _short(name: str) -> str:
    return name.split("/", 1)[-1]


def parse_documents(hld: str) -> list[dict]:
    """'## Documents' lines: repo -> type, formats, max MB, retention, dataset, fields (and problems)."""
    from .agents.parse import section

    out = []
    for repo, rest in _LINE.findall(section(hld, "Documents")):
        parts = [p.strip() for p in rest.split(";")]
        line: dict = {"repo": repo, "type": parts[0].lower(), "formats": [], "max_mb": 0, "keep_days": 0,
                      "dataset": "", "fields": [], "required": [], "problems": []}
        for p in parts[1:]:
            key, _, value = p.partition(" ")
            value = value.strip()
            if key == "formats":
                line["formats"] = [f.strip().lower() for f in value.split(",") if f.strip()]
            elif key == "max":
                m = re.fullmatch(r"(\d+)\s*MB", value, re.I)
                line["max_mb"] = int(m.group(1)) if m else -1
            elif key == "keep":
                m = re.fullmatch(r"(\d+)\s*days?", value, re.I)
                line["keep_days"] = int(m.group(1)) if m else -1
            elif key == "dataset":
                line["dataset"] = value
            elif key == "fields":
                names = [f.strip() for f in value.split(",") if f.strip()]
                line["problems"] += [f"field '{n}' isn't a name (lowercase, underscores; * for required)"
                                     for n in names if not _FIELD.match(n)]
                line["fields"] = [n.rstrip("*") for n in names]
                line["required"] = [n.rstrip("*") for n in names if n.endswith("*")]
        p = line["problems"]
        if not _NAME.match(line["type"]):
            p.append("name the document type first (lowercase, dashes)")
        if not line["formats"] or any(f not in FORMATS for f in line["formats"]):
            p.append(f"'formats <{', '.join(FORMATS)}>': read from the content, so name only these")
        if not 1 <= line["max_mb"] <= 50:
            p.append("'max <1-50> MB': the largest upload taken")
        if line["keep_days"] < 1:
            p.append("'keep <N> days': how long documents of this type are kept (the retention rule)")
        if not _NAME.match(line["dataset"]):
            p.append("'dataset <name>': the contract its fields are published under")
        if not line["fields"]:
            p.append("'fields <name>*, <name>': what's read from it (* for required)")
        out.append(line)
    return out


def design_findings(hld: str, repos: list[dict]) -> list[str]:
    """G4: every intake repo says what it takes, has its two buckets, and isn't reached directly."""
    from .agents.parse import parse_endpoints
    from .resources import parse_resources

    intakes = {r["name"] for r in repos if r.get("role") == ROLE}
    out, named = [], set()
    for line in parse_documents(hld):
        named.add(line["repo"])
        if line["repo"] not in intakes:
            out.append(f"Documents: {line['repo']} isn't a document intake repo in this design (role {ROLE})")
        out += [f"Documents: {line['repo']}: {line['type'] or '?'}: {p}" for p in line["problems"]]
    for repo in sorted(intakes - named):
        out.append(f"{repo} is a document intake: say what it takes under '## Documents' ('- {repo}: <type>; "
                   "formats pdf, png; max <N> MB; keep <N> days; dataset <name>; fields <name>*, <name>')")
    buckets = [r for r in parse_resources(hld) if r["type"] == "s3-bucket"]
    for repo in sorted(intakes):
        mine = [b for b in buckets if {"read", "write", "delete"} <= b["access"].get(_short(repo), set())]
        if len(mine) < 2:
            out.append(f"{repo} needs two s3-buckets in Resources that it reads, writes and deletes: a quarantine "
                       "for uploads and a clean bucket for checked files")
        elif not any(isinstance(b["params"].get("expire_days"), int) and b["params"]["expire_days"] <= 7
                     for b in mine):
            out.append(f"{repo}'s quarantine bucket needs expire_days of at most 7, so abandoned uploads go")
    for e in parse_endpoints(hld):
        if e["repo"] in intakes and e["visibility"] != ["project"]:
            out.append(f"{e['repo']} is a document intake: people reach it through the product's BFF, so its "
                       "visibility is project (VX-17)")
    return out


def coder_instruction(hld: str, repo: str) -> str:
    lines = [x for x in parse_documents(hld) if x["repo"] == repo and not x["problems"]]
    if not lines:
        return ""
    said = "; ".join(f"{x['type']}: formats {', '.join(x['formats'])}, max_mb {x['max_mb']}, retention_days "
                     f"{x['keep_days']}, dataset {x['dataset']}, fields {', '.join(x['fields'])} (required: "
                     f"{', '.join(x['required']) or 'none'})" for x in lines)
    return (f" intake.yaml's documents are exactly the HLD's Documents lines for this repo: {said}. Set each "
            "field's min_confidence; uploads go to quarantine through presigned POSTs, every file is sized, "
            "sniffed and scanned before it's read, downloads are one-minute links, and every read is checked "
            "against the caller's tenant (VX-15, VX-17).")


def config_findings(text: str, lines: list[dict]) -> list[str]:
    """intake.yaml (on a branch) against its approved lines: anything more is a finding."""
    try:
        raw = yaml.safe_load(text or "")
    except yaml.YAMLError:
        raw = None
    if not isinstance(raw, dict):
        return [f"{PATH} is missing or isn't a YAML mapping"]
    approved = {x["type"]: x for x in lines}
    out = []
    for d in raw.get("documents") or []:
        if not isinstance(d, dict):
            continue
        t = str(d.get("type") or "")
        a = approved.get(t)
        if a is None:
            out.append(f"{PATH} takes {t!r}, which the HLD's Documents section doesn't approve")
            continue
        extra = sorted(set(str(f).lower() for f in d.get("formats") or []) - set(a["formats"]))
        if extra:
            out.append(f"{PATH}: {t} takes formats the HLD doesn't approve: {', '.join(extra)}")
        if int(d.get("max_mb") or 0) > a["max_mb"]:
            out.append(f"{PATH}: {t} takes up to {d.get('max_mb')} MB; the HLD approved {a['max_mb']}")
        if int(d.get("retention_days") or 0) != a["keep_days"]:
            out.append(f"{PATH}: {t} keeps documents {d.get('retention_days')} days; the HLD says {a['keep_days']}")
        if str(d.get("dataset") or "") != a["dataset"]:
            out.append(f"{PATH}: {t} publishes under {d.get('dataset')!r}; the HLD says {a['dataset']}")
        fields = {str(f.get("name")) for f in d.get("fields") or [] if isinstance(f, dict)}
        more = sorted(fields - set(a["fields"]))
        if more:
            out.append(f"{PATH}: {t} reads fields the HLD doesn't approve: {', '.join(more)}")
    return out


def build_findings(github, hld: str, results: dict) -> list[str]:
    lines = [x for x in parse_documents(hld) if not x["problems"]]
    out = []
    for tid, r in sorted(results.items()):
        if r.get("role") != ROLE or not r.get("branch"):
            continue
        mine = [x for x in lines if x["repo"] == r.get("repo")]
        if not mine:
            continue
        try:
            text = github.get_file(repo=r["repo"], path=PATH, ref=r["branch"])
        except Exception:  # noqa: BLE001 - unreadable counts as missing
            text = ""
        out += [f"{tid}: {r['repo']}: {f}" for f in config_findings(text or "", mine)]
    return out
