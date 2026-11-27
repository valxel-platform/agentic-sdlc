"""Data pipelines (role `transform`, valxel-platform/template-transform): what each transform reads,
where it runs and what it publishes, approved at G4 and held to on the branch. The design "Data
pipelines on Valxel" says why.

  the HLD      '## Pipelines', one line per transform repo:
                 '- acme-emr/emr-transform: reads emr.notes; warehouse postgres;
                    marts notes_signed:none, notes_by_clinic_year:none; shares notes_signed with research-partner'
               `reads` names datasets (the Datasets section's contracts), each loaded into the warehouse
               by a reporting repo as a table named after the dataset ('emr.notes' -> notes), or
               `<dataset> as <table>`. `marts` names each mart with the highest data class any of
               its columns may carry. `shares` (optional) names a mart that goes to a destination
               outside the product: only a mart of class none may.
  G4           every transform has its line, and the line holds: each dataset exists, lists the
               transform under its consumers, and is loaded by a reporting repo that keeps at least
               the fields the transform reads; the warehouse is one the template runs on; mart
               classes are real; nothing above none leaves the product. A design that reaches for
               Spark, a streaming engine or a general orchestrator is flagged as unsupported, with
               the reason, rather than improvised (`scope_findings`).
  build        the coder gets the transform's dbt/models/sources.yml, generated here from the contracts
               (each column with its class), and the reporting coder the table names. After the
               coder: sources.yml on the branch is exactly the generated one, transform.yaml names the
               approved warehouse, the marts are exactly the approved ones, none declares a column
               above its approved class, and each has a control total. That the SQL keeps every
               column at least as sensitive as its inputs is the template's own check
               (transform-check, in the repo's required unit check).
  deploy       each Postgres transform gets a login of its own (catalog type postgres-login,
               platform-resources): made in the database the reporting repos it reads load into,
               with SELECT on exactly the columns its contracts give it (and _updated_at), CREATE for
               its own schemas and a break-glass role it administers (`logins`, which
               resources.plan adds). It's bound to the component with fixed variable names
               (LOGIN_ENV), which template-transform reads; the database owner's credentials never
               reach it. A Postgres transform on a cloud without postgres-login, or whose datasets
               aren't loaded into exactly one postgres resource of the design, fails G4
               (`login_findings`).
  staging      reconcile.py reads each transform's valxel.transform.run lines.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from .datasets import CLASSES, RANK, consumers, extensions, fields, parse_hld

ROLE = "transform"
LOGIN_TYPE = "postgres-login"
DATABASE_TYPE = "postgres"
SOURCE_SCHEMA = "public"  # where reporting loads its tables (transform.yaml source_schema)
# postgres-login's outputs -> what template-transform reads (its Settings).
# postgres-login's outputs: names only, never a secret (VX-20). The transform signs in as its own
# workload identity with a token, and tokenizes through the database's function.
LOGIN_ENV = {"host": "TRANSFORM_PG_HOST", "port": "TRANSFORM_PG_PORT", "database": "TRANSFORM_PG_DATABASE",
             "username": "TRANSFORM_PG_USER", "auth": "TRANSFORM_PG_AUTH",
             "break_glass_role": "TRANSFORM_BREAK_GLASS_ROLE", "tokenize_function": "TRANSFORM_TOKENIZE_FUNCTION"}
REPORTING = "reporting"
WAREHOUSES = ("postgres",)  # snowflake comes with template-transform's second step (CP-126)
SOURCES = "dbt/models/sources.yml"
CONFIG = "transform.yaml"
REPORTING_CONFIG = "reporting.yaml"
WORKLOAD = "workload.yaml"
_LINE = re.compile(r"^- ([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)\s*:\s*(.+?)\s*$", re.M)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_DEST = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
# Engines the platform doesn't run (the design's "Out of scope, and when to revisit").
_OUT_OF_SCOPE = {
    r"\b(py)?spark\b": "Spark",
    r"\bdatabricks\b": "Databricks",
    r"\bflink\b": "Flink",
    r"\bapache beam\b": "Apache Beam",
    r"\bairflow\b|\bmwaa\b|\bcloud composer\b": "Airflow",
    r"\bdagster\b": "Dagster",
    r"\bprefect\b": "Prefect",
    r"\bstreaming joins?\b": "streaming joins",
}
_NEGATED = re.compile(r"\b(no|not|without|never|instead of|out of scope|rather than)\b", re.I)


def _short(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


def table_for(dataset: str) -> str:
    """The reporting table a dataset loads into by default: 'emr.notes' -> notes."""
    return re.sub(r"[^a-z0-9_]", "_", dataset.split(".", 1)[-1].lower())


def _parts(rest: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for chunk in rest.split(";"):
        word, _, value = chunk.strip().partition(" ")
        if word:
            parts[word.lower()] = value.strip()
    return parts


def _items(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _parse_reads(value: str, problems: list[str]) -> list[dict]:
    reads = []
    for item in _items(value):
        dataset, _, table = (p.strip() for p in item.partition(" as "))
        table = table or table_for(dataset)
        if not _IDENT.match(table):
            problems.append(f"reads {item}: the table is a lowercase SQL name")
        reads.append({"dataset": dataset, "table": table})
    if not reads:
        problems.append("say what it reads: 'reads <dataset>[ as <table>], ...'")
    return reads


def _parse_marts(value: str, problems: list[str]) -> dict[str, str]:
    marts: dict[str, str] = {}
    for item in _items(value):
        name, _, cls = (p.strip().lower() for p in item.partition(":"))
        if not _IDENT.match(name) or name in marts:
            problems.append(f"marts {item}: name each mart once, as a lowercase SQL name")
        if cls not in RANK:
            problems.append(f"marts {item}: give the mart's highest data class ({', '.join(CLASSES)})")
        marts[name] = cls
    if not marts:
        problems.append("name its marts: 'marts <name>:<data class>, ...'")
    return marts


def _parse_shares(value: str, marts: dict[str, str], problems: list[str]) -> list[dict]:
    shares = []
    for item in _items(value):
        mart, _, dest = (p.strip().lower() for p in item.partition(" with "))
        if mart not in marts or not _DEST.match(dest):
            problems.append(f"shares {item}: '<one of its marts> with <destination>'")
        shares.append({"mart": mart, "destination": dest})
    return shares


def parse_pipelines(hld: str) -> list[dict]:
    """'## Pipelines' lines: repo -> reads, warehouse, marts, shares (and any problems)."""
    from .agents.parse import section

    out = []
    for repo, rest in _LINE.findall(section(hld, "Pipelines")):
        problems: list[str] = []
        parts = _parts(rest)
        reads = _parse_reads(parts.get("reads", ""), problems)
        marts = _parse_marts(parts.get("marts", ""), problems)
        shares = _parse_shares(parts.get("shares", ""), marts, problems)
        out.append({"repo": repo, "reads": reads, "warehouse": parts.get("warehouse", "").lower(), "marts": marts,
                    "shares": shares, "problems": problems})
    return out


def _reads(c: dict, repo: str) -> list[str]:
    """The properties a contract lets `repo` read (consumers keyed by full or short name)."""
    mine = consumers(c)
    return mine.get(repo) or mine.get(_short(repo)) or []


def _read_findings(repo: str, read: dict, contracts: dict[str, dict], reporting: set[str]) -> list[str]:
    c = contracts.get(read["dataset"])
    if c is None:
        return [f"Pipelines: {repo} reads {read['dataset']}, which isn't a contract under '## Datasets': "
                "a mart is built only from approved datasets"]
    out = []
    wanted = _reads(c, repo)
    if not wanted:
        out.append(f"Pipelines: {repo} reads {read['dataset']}, but the contract doesn't list "
                   f"{_short(repo)} under consumers with the fields it reads")
    loaders = [r for r in reporting if _reads(c, r)]
    if not loaders:
        out.append(f"Pipelines: nothing loads {read['dataset']} into the warehouse for {repo}: "
                   "a reporting repo in this design must consume it")
    elif not any(set(wanted) <= set(_reads(c, r)) for r in loaders):
        missing = sorted(set(wanted) - set().union(*(set(_reads(c, r)) for r in loaders)))
        out.append(f"Pipelines: {repo} reads {', '.join(missing)} of {read['dataset']}, which no "
                   "reporting repo keeps: list them under that reporting repo's consumers too")
    return out


def _share_findings(line: dict) -> list[str]:
    out = []
    for share in line["shares"]:
        cls = line["marts"].get(share["mart"], "")
        if cls in RANK and RANK[cls] > RANK["none"]:
            out.append(f"Pipelines: {line['repo']} shares {share['mart']} ({cls}) with {share['destination']}: only "
                       "a mart of class none leaves the product. De-identify it in the models "
                       "(valxel_generalize_year, an aggregate under valxel_min_group; valxel_tokenize only with "
                       "a recorded Expert Determination) and give it class none")
    return out


def design_findings(hld: str, repos: list[dict]) -> list[str]:
    """G4: every transform has a Pipelines line, and each line holds against the contracts."""
    roles = {r["name"]: r.get("role") for r in repos}
    transforms = {n for n, role in roles.items() if role == ROLE}
    reporting = {n for n, role in roles.items() if role == REPORTING}
    contracts = {str(c.get("id")): c for c in parse_hld(hld)[0]}
    out = []
    named = set()
    for line in parse_pipelines(hld):
        repo = line["repo"]
        named.add(repo)
        out += [f"Pipelines: {repo}: {p}" for p in line["problems"]]
        if repo not in transforms:
            out.append(f"Pipelines: {repo} isn't a transform repo in this design (role transform)")
            continue
        if line["warehouse"] not in WAREHOUSES:
            out.append(f"Pipelines: {repo}: warehouse must be {' or '.join(WAREHOUSES)} for now "
                       "(snowflake comes with the transform template's Snowflake step)")
        for read in line["reads"]:
            out += _read_findings(repo, read, contracts, reporting)
        out += _share_findings(line)
    for repo in sorted(transforms - named):
        out.append(f"{repo} is a transform: say what it reads and publishes under '## Pipelines' ('- {repo}: "
                   "reads <dataset>, ...; warehouse postgres; marts <name>:<data class>, ...')")
    return out + scope_findings(hld)


def scope_findings(hld: str) -> list[str]:
    """G4: engines the platform doesn't run are flagged, not improvised. A line that rules one out
    ('no Spark', 'instead of Airflow') isn't a finding."""
    from .agents.parse import split_sections

    out: list[str] = []
    seen: set[str] = set()
    _, sections = split_sections(hld or "")
    for heading, body in sections:
        if heading.strip().lower() in ("non-goals", "adrs"):
            continue
        for text in body.splitlines():
            if _NEGATED.search(text):
                continue
            for pattern, name in _OUT_OF_SCOPE.items():
                if name not in seen and re.search(pattern, text, re.I):
                    seen.add(name)
                    out.append(
                        f"{heading.strip() or 'HLD'}: the design uses {name}, which the platform doesn't run: "
                        "pipelines are connectors, reporting and transforms (dbt on the job harness). If the product "
                        "truly needs it, say why and raise an ADR for the platform team instead"
                    )
    return out


def login_resource(repo: str) -> str:
    """The transform's login, as an OpenChoreo Resource: '<component>-login'."""
    return f"{_short(repo)}-login"


def login_role(repo: str) -> str:
    """The login's role name in the database: the component's name as a SQL name."""
    name = re.sub(r"[^a-z0-9_]", "_", _short(repo).lower())
    return (name if name[:1].isalpha() else f"t_{name}")[:50]


def _databases(line: dict, contracts: dict[str, dict], entries: list[dict]) -> list[dict]:
    """The design's postgres resources that a repo loading one of the line's datasets uses."""
    comp = _short(line["repo"])
    out = []
    for e in entries:
        if e["type"] != DATABASE_TYPE:
            continue
        loaders = {_short(c) for c in e["access"]} - {comp}
        datasets = [contracts[r["dataset"]] for r in line["reads"] if r["dataset"] in contracts]
        if any(_short(k) in loaders and v for c in datasets for k, v in consumers(c).items()):
            out.append(e)
    return out


def _login_reads(line: dict, contracts: dict[str, dict]) -> list[dict]:
    reads = []
    for read in line["reads"]:
        c = contracts.get(read["dataset"])
        if c is None:
            continue
        props = fields(c)
        cols = [f for f in _reads(c, line["repo"]) if f in props]
        reads.append({"schema": SOURCE_SCHEMA, "table": read["table"], "columns": [*cols, "_updated_at"]})
    return reads


def logins(hld: str, catalog: dict) -> list[dict]:
    """One login per Postgres transform whose line holds: component, resource name, and the
    postgres-login parameters (database, login, reads, createSchemas, breakGlass)."""
    from .resources import resolved

    if LOGIN_TYPE not in (catalog.get("types") or {}):
        return []
    contracts = {str(c.get("id")): c for c in parse_hld(hld)[0]}
    entries = resolved(hld, catalog)
    out = []
    for line in parse_pipelines(hld):
        if line["problems"] or line["warehouse"] != "postgres":
            continue
        dbs = _databases(line, contracts, entries)
        if len(dbs) != 1 or "database" not in dbs[0]["params"]:
            continue
        out.append({"component": _short(line["repo"]), "name": login_resource(line["repo"]),
                    "database_resource": dbs[0]["name"],
                    "parameters": {"database": str(dbs[0]["params"]["database"]), "login": login_role(line["repo"]),
                                   "component": _short(line["repo"]), "tokenize": True,
                                   "reads": _login_reads(line, contracts), "createSchemas": True,
                                   "breakGlass": True}})
    return out


def login_findings(hld: str, catalog: dict) -> list[str]:
    """G4: a Postgres transform can get a login of its own here, in exactly one database."""
    from .resources import cloud_name, resolved

    lines = [p for p in parse_pipelines(hld) if not p["problems"] and p["warehouse"] == "postgres"]
    if not lines or not catalog.get("types"):
        return []
    if LOGIN_TYPE not in catalog["types"]:
        return [f"Pipelines: {', '.join(p['repo'] for p in lines)}: a Postgres transform signs in with a login of "
                f"its own (postgres-login), which the platform doesn't offer on {cloud_name(catalog)} yet"]
    contracts = {str(c.get("id")): c for c in parse_hld(hld)[0]}
    entries = resolved(hld, catalog)
    out = []
    for line in lines:
        dbs = _databases(line, contracts, entries)
        if len(dbs) != 1:
            found = ", ".join(e["name"] for e in dbs) or "none"
            out.append(f"Pipelines: {line['repo']} reads from the database its reporting repos load into: exactly one "
                       f"postgres resource under '## Resources' must be used by them (found: {found})")
        elif "database" not in dbs[0]["params"]:
            out.append(f"Pipelines: {line['repo']}: {dbs[0]['name']} needs its database= parameter")
    return out


def login_snippet(repo: str) -> str:
    """The transform's workload.yaml entry for its login."""
    lines = [f"    - ref: {login_resource(repo)}", "      envBindings:"]
    return "\n".join(lines + [f"        {out}: {env}" for out, env in LOGIN_ENV.items()])


def sources_yml(hld: str, repo: str) -> str:
    """The transform's dbt/models/sources.yml, from the approved contracts: one source table per
    dataset it reads, only the fields it may read, each with its data class."""
    line = next((p for p in parse_pipelines(hld) if p["repo"] == repo), None)
    if line is None:
        return ""
    contracts = {str(c.get("id")): c for c in parse_hld(hld)[0]}
    tables = []
    for read in line["reads"]:
        c = contracts.get(read["dataset"])
        if c is None:
            continue
        props = fields(c)
        keys = [n for n, p in props.items() if p.get("primaryKey")]
        wanted = [f for f in _reads(c, repo) if f in props]
        meta: dict[str, Any] = {"dataset": read["dataset"]}
        if keys:
            meta["key"] = keys[0]
        tenant = extensions(c).get("tenant")
        if tenant:
            meta["tenant"] = str(tenant)
        columns = [{"name": f, "meta": {"data_class": str(props[f].get("classification") or "secret")}}
                   for f in wanted]
        columns.append({"name": "_updated_at", "meta": {"data_class": "none"}})
        tables.append({"name": read["table"], "meta": meta, "columns": columns})
    doc = {"version": 2, "sources": [{
        "name": "reporting",
        "schema": "{{ env_var('TRANSFORM_SOURCE_SCHEMA', 'public') }}",
        "tables": tables,
    }]}
    head = ("# Written by the Valxel SDLC from the datasets the design lets this transform read; don't edit by\n"
            "# hand. Each column carries its data class from the dataset's contract: models can only source()\n"
            "# these, and transform-check classes every output from them.\n")
    return head + yaml.safe_dump(doc, sort_keys=False, width=120)


def coder_instruction(hld: str, repo: str) -> str:
    """What the coder of a transform repo, or of a reporting repo a transform reads, is told."""
    lines = parse_pipelines(hld)
    mine = next((p for p in lines if p["repo"] == repo and not p["problems"]), None)
    if mine is not None:
        marts = ", ".join(f"{n} (no column above {c})" for n, c in mine["marts"].items())
        shared = "".join(f" {s['mart']} goes to {s['destination']}: every column of it must be class none."
                         for s in mine["shares"])
        return (f" Write {SOURCES} exactly as below; models read only source('reporting', ...) of those tables."
                f" transform.yaml's warehouse is {mine['warehouse']}. Build exactly these marts under"
                f" dbt/models/marts/ (<name>.sql), each declared in marts.yml: {marts}.{shared}"
                " Every model column declares meta.data_class, at least as"
                " sensitive as what it's made from (de-identify only with valxel_generalize_year or an aggregate"
                " under valxel_min_group(11); a valxel_tokenize column keeps its source's class unless"
                " transform.yaml records an expert_determination, which only a person adds); every mart has a"
                " valxel_control_total against what it's built from; an incremental model removes what's gone"
                " at the source with the quoted valxel_deletions hook. uv run transform-check must pass.\n\n"
                f"```yaml\n{sources_yml(hld, repo)}```\n"
                + (f"It signs in as its own workload identity with a login made when it deploys: no password."
                   f" workload.yaml binds it under dependencies.resources exactly so:\n\n```yaml\n"
                   f"{login_snippet(repo)}\n```\n" if mine["warehouse"] == "postgres" else ""))
    tables = sorted({(r["table"], r["dataset"]) for p in lines if not p["problems"] for r in p["reads"]})
    if not tables:
        return ""
    contracts = {str(c.get("id")) for c in parse_hld(hld)[0] if _reads(c, repo)}
    mine_tables = [(t, d) for t, d in tables if d in contracts]
    if not mine_tables:
        return ""
    named = ", ".join(f"{t} (from: {d})" for t, d in mine_tables)
    return (f" A transform reads these reporting tables: name them exactly {named} in reporting.yaml's tables,"
            " each from its dataset, keeping the fields the contract lists for this repo.")


def config_findings(
    sources: str, config: str, marts: dict[str, dict[str, str]], line: dict, expected_sources: str
) -> list[str]:
    """A transform branch against its approved line. `marts` is model name -> the .yml's columns'
    classes, for the models under dbt/models/marts/ (with 'control_total': '1' when it has one)."""
    out = []
    if _normal(sources) != _normal(expected_sources):
        out.append(f"{SOURCES} differs from what the design approves: write it exactly as the tool gives it")
    try:
        warehouse = str((yaml.safe_load(config or "") or {}).get("warehouse") or "")
    except yaml.YAMLError:
        warehouse = ""
    if warehouse != line["warehouse"]:
        out.append(f"{CONFIG} says warehouse {warehouse or 'nothing'}; the HLD approved {line['warehouse']}")
    approved = line["marts"]
    for name in sorted(set(marts) - set(approved)):
        out.append(f"mart {name} isn't in the HLD's Pipelines line: a new mart is a design change")
    for name in sorted(set(approved) - set(marts)):
        out.append(f"mart {name} is approved but not built: dbt/models/marts/{name}.sql, declared in marts.yml")
    for name, cols in sorted(marts.items()):
        if name not in approved:
            continue
        if not cols.pop("control_total", ""):
            out.append(f"mart {name} has no valxel_control_total against what it's built from")
        high = [f"{c} ({k})" for c, k in cols.items() if k in RANK and RANK[k] > RANK[approved[name]]]
        if high:
            out.append(f"mart {name} declares {', '.join(high)} above its approved class {approved[name]}")
    return out


def _normal(text: str) -> Any:
    try:
        return yaml.safe_load(text or "")
    except yaml.YAMLError:
        return text


MARTS = "dbt/models/marts/marts.yml"


def _declared(doc: Any) -> dict[str, dict[str, str]]:
    """Model -> column -> declared class (and 'control_total': '1' with one) from a dbt .yml."""
    out: dict[str, dict[str, str]] = {}
    for m in doc.get("models") or [] if isinstance(doc, dict) else []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        cols = out.setdefault(str(m["name"]), {})
        for c in m.get("columns") or []:
            if isinstance(c, dict):
                cols[str(c.get("name"))] = str((c.get("meta") or {}).get("data_class") or "")
        tests = m.get("data_tests") or m.get("tests") or []
        if any(isinstance(t, dict) and "valxel_control_total" in t for t in tests):
            cols["control_total"] = "1"
    return out


def _marts(github, repo: str, ref: str, approved: dict[str, str]) -> dict[str, dict[str, str]]:
    """The marts dbt/models/marts/marts.yml declares on the branch (every mart is declared there:
    transform-check fails a model whose columns aren't), less an approved one whose SQL isn't there."""

    def text(path: str) -> str:
        try:
            return github.get_file(repo=repo, path=path, ref=ref) or ""
        except Exception:  # noqa: BLE001 - unreadable counts as missing
            return ""

    try:
        out = _declared(yaml.safe_load(text(MARTS)))
    except yaml.YAMLError:
        out = {}
    for name in approved:
        if name in out and not text(f"dbt/models/marts/{name}.sql"):
            out.pop(name)  # declared but not built
    return out


def _reporting_findings(tid: str, text: str, tables: list[tuple[str, str]]) -> list[str]:
    try:
        cfg = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        cfg = {}
    raw = cfg.get("tables") if isinstance(cfg, dict) else None
    have = {str(n): str((t or {}).get("from") or "") for n, t in (raw or {}).items()} if isinstance(raw, dict) else {}
    return [f"{tid}: {REPORTING_CONFIG} needs table {table} from {dataset}: a transform reads it"
            for table, dataset in tables if have.get(table) != dataset]


def workload_findings(text: str, repo: str) -> list[str]:
    """The transform's workload.yaml binds its own login, under the variables the template reads,
    and no other database: the owner's credentials (a postgres resource) never reach it."""
    try:
        doc = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return [f"{WORKLOAD} doesn't parse"]
    deps = ((doc.get("dependencies") or {}).get("resources") or []) if isinstance(doc, dict) else []
    refs = {str(d.get("ref")): d for d in deps if isinstance(d, dict)}
    want = login_resource(repo)
    out = []
    if want not in refs:
        out.append(f"{WORKLOAD} must bind {want} under dependencies.resources (its own login)")
    elif (refs[want].get("envBindings") or {}) != LOGIN_ENV:
        out.append(f"{WORKLOAD}: {want}'s envBindings must be exactly {LOGIN_ENV}")
    others = sorted(r for r in refs if r != want)
    if others:
        out.append(f"{WORKLOAD} binds {', '.join(others)}: a transform binds only its own login")
    return out


def build_findings(github, hld: str, results: dict) -> list[str]:
    """After the coders: each transform branch stays within its Pipelines line, and each reporting
    branch a transform reads names the tables the transform's sources.yml points at."""
    lines = {p["repo"]: p for p in parse_pipelines(hld) if not p["problems"]}
    if not lines:
        return []

    def read(repo: str, path: str, ref: str) -> str:
        try:
            return github.get_file(repo=repo, path=path, ref=ref) or ""
        except Exception:  # noqa: BLE001 - unreadable counts as missing
            return ""

    tables = sorted({(r["table"], r["dataset"]) for p in lines.values() for r in p["reads"]})
    contracts = {str(c.get("id")): c for c in parse_hld(hld)[0]}
    out = []
    for tid, r in sorted(results.items()):
        repo, ref = r.get("repo"), r.get("branch")
        if not ref:
            continue
        if r.get("role") == ROLE and repo in lines:
            built = _marts(github, repo, ref, lines[repo]["marts"])
            out += [f"{tid}: {f}" for f in config_findings(
                read(repo, SOURCES, ref), read(repo, CONFIG, ref), built, lines[repo], sources_yml(hld, repo))]
            if lines[repo]["warehouse"] == "postgres":
                out += [f"{tid}: {f}" for f in workload_findings(read(repo, WORKLOAD, ref), repo)]
        elif r.get("role") == REPORTING:
            mine = [(t, d) for t, d in tables if d in contracts and _reads(contracts[d], repo)]
            if mine:
                out += _reporting_findings(tid, read(repo, REPORTING_CONFIG, ref), mine)
    return out
