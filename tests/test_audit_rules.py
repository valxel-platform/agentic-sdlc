"""VX-15: PHI and payments designs name what gets audited, and the build publishes those records."""

from vrixel_sdlc.agents.parse import parse_audit
from vrixel_sdlc.evaluators.checks import run_checks
from vrixel_sdlc.graph.phase import audit_findings, audit_instruction

HLD = """# HLD: Consent capture

## ADRs
- ADR-CLN-FEAT-1-1: Extend the intake API

## Affected repos
- acme-clinic/cln-intake-api (provider)

## Contracts
- acme-clinic/cln-intake-api: 1.2.0 -> 1.3.0

## Diagram
```mermaid
flowchart LR
  api[cln-intake-api]
```
{audit}"""

AUDIT = """
## Audit
- consent.read: consent (who viewed which consent form)
- consent.change: consent
"""


def ctx(data_class: str) -> dict:
    return {"data_class": data_class, "repos": [{"name": "acme-clinic/cln-intake-api", "role": "provider"}]}


def test_the_audit_section_is_parsed() -> None:
    assert parse_audit(AUDIT) == [{"action": "consent.read", "record": "consent"},
                                  {"action": "consent.change", "record": "consent"}]
    assert parse_audit("## Audit\n- not an action\n") == []


def test_phi_and_payments_designs_must_name_their_audit_records() -> None:
    missing = "HLD has no Audit section"
    assert any(missing in f for f in run_checks("architecture", HLD.format(audit=""), ctx("phi")))
    assert any(missing in f for f in run_checks("architecture", HLD.format(audit=""), ctx("payments")))
    assert not any(missing in f for f in run_checks("architecture", HLD.format(audit=AUDIT), ctx("phi")))
    assert not any(missing in f for f in run_checks("architecture", HLD.format(audit=""), ctx("general")))


def test_the_build_must_publish_what_the_design_names() -> None:
    c = {"audit": parse_audit(AUDIT)}
    provider = {"role": "provider", "repo": "o/api", "tests_passed": True}
    ok = {"T1": {**provider, "summary": "Audit: consent.read, consent.change"}}
    half = {"T1": {**provider, "summary": "Audit: consent.read"}}
    web = {"T1": {"role": "consumer", "repo": "o/web", "tests_passed": True, "summary": ""}}
    assert audit_findings(c, ok) == [] and audit_findings(c, web) == [] and audit_findings({}, half) == []
    assert audit_findings(c, half) == ["No task publishes the audit records the HLD names: consent.change (VX-15)"]
    assert "consent.read (consent)" in audit_instruction(c, {"role": "mcp-server"})
    assert audit_instruction(c, {"role": "consumer"}) == "" and audit_instruction({}, {"role": "provider"}) == ""
