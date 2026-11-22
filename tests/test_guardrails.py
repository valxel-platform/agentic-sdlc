"""RC5 of the engineering review: the lint gate (finding 11) and security headers (finding 15)."""

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from valxel_sdlc.api.app import create_app
from valxel_sdlc.web.auth import AuthConfig

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lint_ratchet", ROOT / "scripts/lint_ratchet.py")
ratchet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ratchet)


def test_the_ratchet_fails_on_a_new_finding_and_passes_on_fewer(tmp_path, monkeypatch, capsys):
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps({"src/a.py C901": 2, "src/b.py mypy:arg-type": 1}))
    monkeypatch.setattr(ratchet, "BASELINE", base)
    monkeypatch.setattr(ratchet, "ROOT", tmp_path)

    def found(*rows):
        return lambda: [{"path": p, "line": i, "rule": r, "msg": "m"} for i, (p, r) in enumerate(rows)]

    monkeypatch.setattr(ratchet, "ruff", found(("src/a.py", "C901"), ("src/a.py", "C901")))
    monkeypatch.setattr(ratchet, "mypy", found())
    assert ratchet.main([]) == 0 and "1 fewer than the baseline" in capsys.readouterr().out
    monkeypatch.setattr(ratchet, "mypy", found(("src/c.py", "mypy:attr-defined")))
    assert ratchet.main([]) == 1
    out = capsys.readouterr().out
    assert "src/c.py: mypy:attr-defined 0 -> 1" in out and "src/c.py:0: m" in out
    assert ratchet.main(["--update"]) == 0
    assert json.loads(base.read_text()) == {"src/a.py C901": 2, "src/c.py mypy:attr-defined": 1}


def test_mypy_lines_are_read_with_their_codes():
    m = ratchet.MYPY_LINE.match('src/valxel_sdlc/x.py:12: error: Incompatible types  [assignment]')
    assert m and m["path"] == "src/valxel_sdlc/x.py" and m["code"] == "assignment"


@pytest.mark.skipif(not (shutil.which("ruff") and shutil.which("mypy")), reason="needs ruff and mypy (.[lint])")
def test_the_code_is_no_worse_than_the_baseline():
    assert ratchet.main([]) == 0


def test_responses_carry_the_security_headers(engine):
    for url, hsts in (("https://valxel.example", True), ("http://localhost:8000", False)):
        with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40, public_url=url),
                                   web_dist=None)) as c:
            h = c.get("/api/healthz").headers
        assert "frame-ancestors 'none'" in h["content-security-policy"] and h["x-frame-options"] == "DENY"
        assert h["x-content-type-options"] == "nosniff"
        assert ("strict-transport-security" in h) is hsts
