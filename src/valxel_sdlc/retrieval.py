"""The knowledge role: retrieval over a product's own records for its agents
(valxel-platform/template-knowledge; the knowledge template design).

Valxel builds a product's knowledge repo itself, during the product build:

  architecture  When an HLD needs search over product text, the architecture agent lists a
                `knowledge` repo and says, under '## Knowledge', which embedding model it uses and
                which sources it indexes. People decide the model at G4: a provider under the BAA
                through the gateway, or the product's self-hosted model, for PHI. For each source
                the agent also decides how a rebuild gets every record: a compacted topic (Kafka
                keeps each record's latest event; the events carry whole records keyed by id), or
                a scan of the service's database. People approve that at G4 too.
  build         Coder agents write the repo's knowledge.yaml from that line and the product's data
                model, and draft the golden set (evals/questions.yaml). The provider that publishes
                a compacted source keys its events by record id, carries whole records and declares
                the topic compacted. People approve all of it with the code at G5.
  every PR      The `retrieval` suite: recall of the expected passages, and never a passage for
                someone who must not see it or from a deleted record.
  G6            The release's recall beside the last release's (graph.pipeline.agent_evals).
  production    A source more than an hour behind raises the knowledge-lag alert (alert-rules.yaml).
"""

from __future__ import annotations

import re

from .agents.parse import bullets, section

# - acme-clinic/emr-knowledge: model text-embedding-3-small via gateway (BAA);
#   sources: notes (events emr.notes.v1, compacted), policies (scan clinic_documents), forms (files)
LINE = re.compile(
    r"^(?P<repo>[\w.-]+/[\w.-]+):\s*model\s+(?P<model>[^;]+?)\s+via\s+(?P<via>[^;]+?)\s*;\s*sources:\s*(?P<sources>.+)$",
    re.IGNORECASE,
)
SOURCE = re.compile(r"(?P<name>[a-z][a-z0-9_-]*)\s*\((?P<kind>events|scan|files)\b(?P<rest>[^)]*)\)", re.IGNORECASE)
VIAS = ("gateway (baa)", "gateway", "self-hosted")
# EVAL_SUITES in graph.pipeline: the knowledge repo's golden set on the PR, live in dev.
SUITES = {"pr": "retrieval", "dev": "integration"}


def parse_knowledge(text: str) -> list[dict]:
    """The HLD's Knowledge lines: repo, embedding model, how it's reached, and its sources."""
    out = []
    for b in bullets(section(text, "Knowledge")):
        m = LINE.match(b.strip())
        if not m:
            continue
        sources = []
        for s in SOURCE.finditer(m["sources"]):
            detail = s["rest"].strip()
            words = [w.strip() for w in detail.replace(",", " ").split() if w.strip()]
            sources.append({"name": s["name"].lower(), "kind": s["kind"].lower(), "detail": detail,
                            "topic": words[0] if s["kind"].lower() == "events" and words else "",
                            "compacted": "compacted" in (w.lower() for w in words)})
        out.append({"repo": m["repo"], "model": m["model"].strip(), "via": m["via"].strip().lower(), "sources": sources})
    return out


def check(text: str, repos: list[dict], ctx: dict) -> list[str]:
    """Each knowledge repo in the design names its model, how it's reached and its sources, and a
    PHI product embeds only under the BAA or with its own model."""
    mine = [r for r in repos if r.get("role") == "knowledge"]
    if not mine:
        return []
    lines = {k["repo"]: k for k in parse_knowledge(text)}
    out = []
    for r in mine:
        k = lines.get(r["name"])
        if k is None:
            out.append(f"{r['name']}: say under '## Knowledge' which embedding model it uses and which sources it "
                       "indexes: '- <org>/<repo>: model <model> via <gateway (BAA)|gateway|self-hosted>; sources: "
                       "<name> (<events <topic>|scan <store>|files>), ...' (people decide the model at G4)")
            continue
        if k["via"] not in VIAS:
            out.append(f"{r['name']}: reach the embedding model via {', '.join(VIAS)}, not {k['via']!r}")
        elif ctx.get("data_class") == "phi" and k["via"] == "gateway":
            out.append(f"{r['name']}: a PHI product embeds through the gateway under the BAA ('gateway (BAA)') or "
                       "with its self-hosted model")
        if not k["sources"]:
            out.append(f"{r['name']}: name at least one source it indexes, e.g. 'notes (events emr.notes.v1, compacted)'")
        for s in k["sources"]:
            if s["kind"] != "events":
                continue
            if not s["topic"]:
                out.append(f"{r['name']}: name the topic {s['name']} is read from, e.g. '{s['name']} (events <topic>, compacted)'")
            elif not s["compacted"]:
                # A rebuild (a new embedding model or chunking) replays the topic from its start; with
                # ordinary retention the older records are gone, and the new index silently lacks them.
                out.append(f"{r['name']}: {s['name']} is rebuilt by replaying {s['topic']} from its start; decide how "
                           "it keeps every record: '(events {t}, compacted)' when its events carry whole records keyed "
                           "by id, or '(scan <store>)' when they don't".format(t=s["topic"]))
    return out


def compacted_topics(text: str) -> dict[str, str]:
    """The compacted topics knowledge repos read in this design: topic -> knowledge repo."""
    return {s["topic"]: k["repo"] for k in parse_knowledge(text) for s in k["sources"]
            if s["kind"] == "events" and s["compacted"] and s["topic"]}


def instruction(ctx: dict, task: dict) -> str:
    """What a coder agent writes in a knowledge repo during the build, and what a provider that
    publishes a compacted knowledge source must keep true."""
    hld = (ctx.get("parent_texts") or {}).get("HLD", "")
    if task.get("role") == "provider":
        topics = compacted_topics(hld)
        if not topics:
            return ""
        return (f" {', '.join(sorted(topics))} {'is' if len(topics) == 1 else 'are'} read by a knowledge repo and rebuilt"
                " by replaying from the start: key every event on it by the record's id, carry the whole record in"
                " each event (never only the changed fields), publish a deletion as a '.deleted' event, and declare the"
                " topic with cleanup.policy=compact wherever this repo declares its topics.")
    if task.get("role") != "knowledge":
        return ""
    k = next((x for x in parse_knowledge((ctx.get("parent_texts") or {}).get("HLD", "")) if x["repo"] == task["repo"]),
             None)
    decided = (f" The embedding model decided at G4 is {k['model']} via {k['via']}; set index.embedding_model to it."
               f" Index these sources: {', '.join(s['name'] + ' (' + s['kind'] + ')' for s in k['sources'])}."
               if k else "")
    if k and any(s["compacted"] for s in k["sources"]):
        decided += (" For each compacted source set compacted: true under its events (the worker checks the topic"
                    " really is compacted).")
    return (" Write knowledge.yaml from the HLD's Knowledge section and the product's data model: one source per"
            " record type, with its id and text fields, its tenant field, an allow list from the record's own access"
            " fields (care team, author, assignee) and every field agents must not see under exclude; a store of"
            " files that can't be read as text is a files source, named but not indexed." + decided
            + " Draft evals/questions.yaml, the golden set: a recall question for each kind of record, a leak case for"
              " every tenant and team boundary, and a deletion case. People approve both at G5. List the sources"
              " after 'Knowledge:' in the patch summary.")
