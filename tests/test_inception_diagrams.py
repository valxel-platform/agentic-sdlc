"""The product architecture's diagrams: a context diagram, a deployment diagram and a flow for each
journey, checked against the text, published for the product gate, and committed at setup."""

from __future__ import annotations

import pytest

from test_inception import VISION
from test_inception_gates import BODY, COUNCIL, client, decide, wait_idle_status  # noqa: F401  (fixture)
from test_api import sign_in
from valxel_sdlc.inception import Vision, checks
from valxel_sdlc.inception import diagrams as inception_diagrams
from valxel_sdlc.inception.run import context_for, run
from valxel_sdlc.integrations.diagrams import DiagramRequest, DrawioPublisher, decode_editor_url


@pytest.fixture
def vision():
    return Vision.from_markdown(VISION, org="acme-water")


@pytest.fixture
def drafted(services, vision):
    [b] = run(services, vision)
    [a] = run(services, vision, brief=b.text, through="architecture")
    return b.text, a.text, context_for(services, vision, "architecture", brief=b.text)


def test_the_architecture_carries_three_diagrams_that_agree_with_it(drafted):
    _, text, ctx = drafted
    for heading in checks.DIAGRAMS:
        assert f"## {heading}\n```mermaid" in text
    assert checks.architecture(text, ctx) == []


def test_each_diagram_is_checked_against_the_text(drafted):
    _, text, ctx = drafted
    bad = (text.replace('  bff["bff"]\n', "").replace("mobile --> bff\n  bff --> api", "mobile --> api")
           .replace('    events_prod["events (scales on cpu)"]\n', "")
           .replace('subgraph cp["OpenChoreo control plane (shared)"]', 'subgraph cp["Shared"]')
           .replace("title J1: Use the product", "title Using it")
           .replace("participant records", "participant billing")
           .replace("api->>records: read and write", "api->>billing: read and write")
           .replace("  alt records are slow or down", "  opt the records answer").replace("  else success\n", ""))
    found = "\n".join(checks.architecture(bad, ctx))
    assert "Context diagram: doesn't show bff" in found
    assert "Deployment diagram: events isn't in the production data plane" in found
    assert "Deployment diagram: events isn't in the non-production" not in found
    assert "OpenChoreo's shared control plane" in found
    assert "no sequence titled 'J1: ...'" in found
    assert "billing isn't a component, a resource or a node of the context diagram" in found


def test_missing_diagrams_and_failure_paths_are_findings(drafted):
    _, text, ctx = drafted
    start, end = text.index("  alt records are slow or down"), text.index("  else success\n")
    no_failure = (text[:start] + text[end + len("  else success\n"):]).replace("    records-->>api", "  records-->>api")
    no_failure = no_failure.replace("    mobile-->>user: result\n  end\n", "  mobile-->>user: result\n")
    assert any("show what happens when a step fails" in f for f in checks.architecture(no_failure, ctx))
    semi = text.replace("a message to try again in a minute, nothing", "a message to try again; nothing")
    assert any("';', which ends the line" in f for f in checks.architecture(semi, ctx))
    gone = text.replace("## Deployment diagram", "## Where it runs")
    assert "Missing the '## Deployment diagram' section" in checks.architecture(gone, ctx)
    flat = text.replace("flowchart LR\n  user", "sequenceDiagram\n  user", 1)
    assert any(f.startswith("Context diagram: write a mermaid flowchart") for f in checks.architecture(flat, ctx))


def test_drawio_without_a_repo_links_the_editor_and_commits_nothing(services):
    pub = DrawioPublisher(services.github)
    before = dict(services.github.files)
    ref = pub.publish(DiagramRequest(feature_id="AW-context", title="Aqua Watch: context v1",
                                     mermaid="flowchart LR\n  web --> api", repo="", branch="", version=1))
    assert services.github.files == before and "file_url" not in ref
    assert 'value="web"' in decode_editor_url(ref["url"])


def test_publishing_links_both_flowcharts_and_replaces_older_links(services, vision, drafted):
    brief, text, _ = drafted
    once, refs, events = inception_diagrams.publish(services, vision, brief, text, 1)
    assert [r["diagram"] for r in refs] == ["context", "deployment"] and all(r["provider"] == "drawio" for r in refs)
    assert "- [Context v1 in draw.io](https://app.diagrams.net/" in once
    assert [e["kind"] for e in events] == ["diagram_published", "diagram_published"]
    twice, _, _ = inception_diagrams.publish(services, vision, brief, once, 2)
    assert twice.count("## Published diagrams") == 1 and "Context v2" in twice and "Context v1" not in twice


def test_lucid_gets_general_designs_but_not_regulated_ones(services, monkeypatch, vision, drafted):
    brief, text, _ = drafted
    monkeypatch.setitem(services.settings.inception, "diagrams", {"provider": "lucid"})
    out, refs, _ = inception_diagrams.publish(services, vision, brief, text, 1)
    assert len(refs) == 2 and "in Lucid](https://lucid.app/" in out
    assert services.diagrams["lucid"].published[0].title == "Aqua Watch: context v1"
    phi = brief.replace("- general:", "- phi:")
    out, refs, events = inception_diagrams.publish(services, vision, phi, text, 1)
    assert refs == [] and "Not published to Lucid: phi designs" in out and events[0]["kind"] == "diagram_withheld"


def test_the_product_gate_shows_the_diagrams_and_setup_commits_them(client, engine, services):  # noqa: F811
    sign_in(client, "lead", teams=COUNCIL)
    tid = client.post("/api/inceptions", json=BODY).json()["feature_id"]
    decide(client, engine, tid, "approve")
    item = decide(client, engine, tid, "approve")
    assert item["gate"] == "product"
    assert "## Context diagram\n```mermaid" in item["preview"] and "## Published diagrams" in item["preview"]
    assert "# Roadmap" in item["preview"]  # the longer architecture doesn't push the roadmap off the page
    v = wait_idle_status(engine, tid, "set_up")
    assert [d["diagram"] for d in v["diagrams"]] == ["context", "deployment"]
    files = {path: body for (repo, _, path), body in services.github.files.items() if repo == "acme-water/aw-governance"}
    assert files["architecture/diagrams/deployment.drawio"].startswith("<mxfile")
    assert "Production data plane" in files["architecture/diagrams/deployment.drawio"]
