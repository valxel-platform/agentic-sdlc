"""How much of the coder's output is scaffolding a code generator could write instead.

An outside review suggested slice generators in the templates (a controller, its DTOs, repository,
service and tests from one command) to save the coder's output tokens. Before building one per
stack, this measures whether boilerplate is a real cost here: for each repo role, the lines the
coder added in new files that look like scaffolding (SCAFFOLD below), beside everything else it
added, and the coder's output tokens on those repos.

  share           scaffold lines / all lines added (new and changed files)
  tokens_saved    the coder's output tokens x share: about what a generator would save

A role is worth a generator when it has at least MIN_TASKS coder tasks and its share is at least
WORTH (`worth_a_generator`). Lines are a proxy for tokens: the coder also spends output tokens on
its reply and on edits it later undid, which no generator saves.

`valxel scaffolding <product>` prints the report; it reads merged and open pull requests of the
product's features from GitHub, and the coder's calls from the usage ledger.
"""

from __future__ import annotations

from fnmatch import fnmatch

# New files of these shapes are what a slice generator would write, by repo role.
SCAFFOLD: dict[str, tuple[str, ...]] = {
    "provider": ("*Controller.java", "*Dto.java", "*Request.java", "*Response.java", "*Repository.java",
                 "*Entity.java", "*Mapper.java", "*Service.java", "*ControllerTest.java", "*RepositoryTest.java",
                 "*/db/migration/V*.sql"),
    "event-worker": ("*Consumer.java", "*Handler.java", "*Event.java", "*Repository.java", "*ConsumerTest.java",
                     "*/db/migration/V*.sql"),
    "bff": ("*Controller.java", "*Route*.java", "*ControllerTest.java"),
    "consumer": ("*/pages/*.tsx", "*/components/*.tsx", "*/api/*.ts", "*.test.tsx", "*.test.ts"),
    "mobile": ("*/screens/*.tsx", "*/components/*.tsx", "*/api/*.ts", "*.test.tsx", "*.test.ts"),
}
MIN_TASKS = 10
WORTH = 0.30


def is_scaffold(role: str, path: str) -> bool:
    return any(fnmatch(path, p) or fnmatch("/" + path, p) for p in SCAFFOLD.get(role, ()))


def tally(role: str, files: list[dict]) -> dict:
    """One pull request's added lines: in new scaffold-shaped files, other new files, changed files."""
    out = {"scaffold": 0, "new_other": 0, "changed": 0}
    for f in files:
        n = int(f.get("additions") or 0)
        if f.get("status") == "added":
            out["scaffold" if is_scaffold(role, f["path"]) else "new_other"] += n
        elif f.get("status") != "removed":
            out["changed"] += n
    return out


def worth_a_generator(row: dict) -> bool:
    return row["tasks"] >= MIN_TASKS and row["share"] >= WORTH


def report(engine, product_id: str, *, since: str = "") -> dict:
    """Per repo role: coder tasks, their output tokens, the lines added and the scaffold share."""
    services = engine.services
    roles = {r.name: r.role for r in services.snapshot(product_id).product.repos}
    rows: dict[str, dict] = {}
    unread: list[str] = []
    for entry in engine.index.all():
        if entry["product_id"] != product_id or entry.get("kind", "feature") != "feature":
            continue
        fid = entry["feature_id"]
        state = engine._state(fid)
        started = str(((state.get("events") or [{}])[0]).get("at") or "")[:10]
        if not state or (since and started < since):
            continue
        task_repo = {t["id"]: t.get("repo", "") for t in state.get("tasks") or []}
        tokens: dict[str, int] = {}
        for rec in services.ledger.records(fid):
            if rec.role == "coder" and rec.task_id in task_repo:
                repo = task_repo[rec.task_id]
                tokens[repo] = tokens.get(repo, 0) + int(rec.output_tokens)
        for pr in (state.get("pull_requests") or {}).values():
            repo, role = pr.get("repo", ""), roles.get(pr.get("repo", ""), "")
            if not role or not pr.get("number"):
                continue
            files = services.github.pr_files(repo=repo, number=int(pr["number"]))
            if files is None:
                unread.append(f"{repo}#{pr['number']}")
                continue
            row = rows.setdefault(role, {"role": role, "tasks": 0, "output_tokens": 0,
                                         "scaffold": 0, "new_other": 0, "changed": 0})
            row["tasks"] += sum(1 for r in task_repo.values() if r == repo)
            row["output_tokens"] += tokens.get(repo, 0)
            for k, v in tally(role, files).items():
                row[k] += v
    for row in rows.values():
        added = row["scaffold"] + row["new_other"] + row["changed"]
        row["share"] = round(row["scaffold"] / added, 3) if added else 0.0
        row["tokens_saved"] = round(row["output_tokens"] * row["share"])
        row["worth_a_generator"] = worth_a_generator(row)
    return {"product_id": product_id, "since": since, "roles": sorted(rows.values(), key=lambda r: -r["tokens_saved"]),
            "unread": unread, "rule": f"worth a generator: at least {MIN_TASKS} coder tasks and a scaffold share "
                                      f"of at least {WORTH:.0%}"}


def markdown(rep: dict) -> str:
    lines = [f"# Scaffolding in {rep['product_id']}'s coder output" + (f" since {rep['since']}" if rep["since"] else ""),
             "", "| Role | Tasks | Coder output tokens | Scaffold lines | Other new lines | Changed lines | Share "
             "| Tokens a generator would save | Worth a generator |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rep["roles"]:
        lines.append(f"| {r['role']} | {r['tasks']} | {r['output_tokens']:,} | {r['scaffold']:,} | {r['new_other']:,} "
                     f"| {r['changed']:,} | {r['share']:.0%} | {r['tokens_saved']:,} | "
                     f"{'yes' if r['worth_a_generator'] else 'no'} |")
    if not rep["roles"]:
        lines.append("| (no coder pull requests yet) | | | | | | | | |")
    lines += ["", rep["rule"].capitalize() + "."]
    if rep["unread"]:
        lines.append(f"Couldn't read {len(rep['unread'])} pull requests from GitHub: {', '.join(rep['unread'][:10])}.")
    return "\n".join(lines) + "\n"
