"""Coder and reviewer evals on every config pull request, in a sandbox (valxel_sdlc.code_eval_runner, T-16)."""

import json
import os
from contextlib import contextmanager
from pathlib import Path

import yaml
from test_code_evals import _case, merged_repo  # noqa: F401 - the fixture

from valxel_sdlc import code_eval_runner, evals, session_runner
from valxel_sdlc.integrations.github import StubGitHub

SHA = "a" * 40


class _Tokens:
    def __init__(self):
        self.asked = []

    def scoped(self, org, names, permissions):
        self.asked.append((f"{org}/{names[0]}", permissions))
        return f"read-{names[0]}"


class _Box:
    """Stands in for the sandbox: keeps the spec, answers with fixed results."""

    def __init__(self, results=None, ok=True):
        self.specs, self.ok = [], ok
        self.results = results or {"case_id": "c", "phase": "coder", "data_class": "general", "passed": True,
                                   "findings": 0, "must_met": None, "critic_agrees": None, "pairwise": None,
                                   "cost_usd": 0.1, "duration_s": 1.0, "trace_ids": []}

    def run(self, spec):
        self.specs.append(json.loads(json.dumps(spec)))  # it crosses as JSON
        if not self.ok:
            return {"ok": False, "error": "Boom", "transient": False}
        return {"ok": True, "baseline": {"results": [self.results]}, "candidate": {"results": [self.results]}}


def _mint(seen):
    @contextmanager
    def mint(tags, models, budget_usd, ttl_s, **_):
        seen.update(tags=tags, models=models, budget=budget_usd, ttl=ttl_s, revoked=False)
        try:
            yield "https://gateway.example", "gw-session-token"
        finally:
            seen["revoked"] = True
    return mint


def test_the_sandbox_gets_the_cases_both_configs_and_scoped_tokens_only(services, monkeypatch):
    monkeypatch.setenv("VALXEL_LLM_GATEWAY_API_KEY", "the-tools-own-gateway-key")
    services.github.tokens = _Tokens()
    box, seen = _Box(), {}
    cfg = services.settings.config_dir
    out = code_eval_runner.run(services, [_case("coder", SHA), _case("reviewer", SHA)], cfg, cfg,
                               budget_usd=5.0, executor=box, mint=_mint(seen))
    [spec] = box.specs
    assert spec["runtime"] == "eval" and len(spec["cases"]) == 2
    assert set(spec["configs"]) == {"baseline", "candidate"} and "models.yaml" in spec["configs"]["candidate"]
    assert spec["repo_tokens"] == {"acme-farm/farm-api": "read-farm-api"}
    assert services.github.tokens.asked == [("acme-farm/farm-api", {"contents": "read"})]
    assert spec["env"]["ANTHROPIC_API_KEY"] == "gw-session-token"
    assert "the-tools-own-gateway-key" not in json.dumps(spec)  # the tool's key never crosses
    assert seen["budget"] == 10.0 and seen["revoked"] and seen["tags"].data_class == "general"
    assert out["candidate"]["summary"]["all"]["cases"] == 1 and out["baseline"]["results"][0]["passed"]


def test_a_config_pr_posts_a_code_status_when_sandboxes_are_on(services, monkeypatch):
    monkeypatch.setenv("VALXEL_SESSION_EXECUTOR", "kubernetes")
    services.evals.capture(_case("coder", SHA))
    gh, repo = StubGitHub(), "valxel-platform/sdlc-config"
    models = yaml.safe_load((Path(services.settings.config_dir) / "models.yaml").read_text())
    gh.files[(repo, "head1", "config/models.yaml")] = yaml.safe_dump(models)
    sent = []

    def fake_run(svc, cases, base_dir, cand_dir, *, budget_usd, timeout_s):
        sent.append([c.id for c in cases])
        r = {"case_id": cases[0].id, "phase": "coder", "data_class": "general", "passed": True, "findings": 0,
             "must_met": None, "critic_agrees": None, "pairwise": None, "cost_usd": 0.1, "duration_s": 1, "trace_ids": []}
        side = {"results": [r], "summary": evals.summarize([r])}
        return {"baseline": side, "candidate": side}

    monkeypatch.setattr(code_eval_runner, "run", fake_run)
    gate = evals.ConfigGate(services, gh, config_repo=repo, background=False)
    run = gate.start("head1", "")
    assert sent[0][0] == "FARM-FEAT-1:T1:coder" and "coder_injection:claude-md" in sent[0]
    code = [(s["state"]) for s in gh.statuses if s["context"] == code_eval_runner.CONTEXT]
    assert code == ["pending", "success"] and gate.runs[run["id"]]["code"]["status"] == "passed"


def test_without_sandboxes_the_gate_leaves_code_cases_alone(services, monkeypatch):
    monkeypatch.setenv("VALXEL_SESSION_EXECUTOR", "local")
    services.evals.capture(_case("coder", SHA))
    gh = StubGitHub()
    monkeypatch.setattr(code_eval_runner, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    evals.ConfigGate(services, gh, config_repo="valxel-platform/sdlc-config", background=False).start("h", "")
    assert not [s for s in gh.statuses if s["context"] == code_eval_runner.CONTEXT]


def test_the_runner_replays_under_each_config_with_the_tokens_it_was_sent(merged_repo, monkeypatch, tmp_path):  # noqa: F811
    _, sha = merged_repo
    for k in ("VALXEL_EVAL_RUNNER", "VALXEL_SESSION_EXECUTOR", "VALXEL_LLM_GATEWAY_URL", "VALXEL_LLM_GATEWAY_API_KEY",
              "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "VALXEL_DELIVERY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VALXEL_LLM_GATEWAY_API_KEY", "must-not-survive")
    seen = []

    def replay(services, case, **_):
        seen.append((os.environ["VALXEL_EVAL_RUNNER"], os.environ["ANTHROPIC_API_KEY"],
                     services.github.tokens.scoped("acme-farm", ["farm-api"], {"contents": "read"}),
                     yaml.safe_load((Path(services.settings.config_dir) / "models.yaml").read_text()).get("x-side")))
        return {"case_id": case.id, "phase": case.phase, "data_class": "general", "passed": True, "findings": 0,
                "must_met": None, "critic_agrees": None, "pairwise": None, "cost_usd": 0.0, "duration_s": 0.0,
                "trace_ids": []}

    monkeypatch.setattr(evals, "replay", replay)
    from valxel_sdlc.config import load_settings

    models = yaml.safe_load((Path(load_settings().config_dir) / "models.yaml").read_text())
    spec = {"version": 1, "runtime": "eval", "id": "eval-1", "cases": [_case("coder", sha).as_dict()],
            "configs": {"baseline": {"models.yaml": yaml.safe_dump({**models, "x-side": "base"})},
                        "candidate": {"models.yaml": yaml.safe_dump({**models, "x-side": "cand"})}},
            "repo_tokens": {"acme-farm/farm-api": "read-tok"}, "env": {"ANTHROPIC_API_KEY": "gw-tok"},
            "budget_usd": 1.0, "timeout_s": 60}
    out = session_runner.run_spec(spec, tmp_path)
    assert [s[3] for s in seen] == ["base", "cand"]
    assert all(s[:3] == ("1", "gw-tok", "read-tok") for s in seen)
    assert "VALXEL_LLM_GATEWAY_API_KEY" not in os.environ
    assert out["candidate"]["summary"]["all"]["cases"] == 1


def test_a_spec_cannot_write_outside_the_config_folder(tmp_path):
    import pytest

    spec = {"version": 1, "runtime": "eval", "cases": [], "configs": {"baseline": {"../x.yaml": ""}, "candidate": {}}}
    with pytest.raises(ValueError, match="not a config file name"):
        code_eval_runner.run_spec(spec, tmp_path)
