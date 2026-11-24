"""Coder and reviewer evals for every config pull request, in a sandbox of their own (tracker T-16).

A coder replay runs model-written code (valxel_sdlc.code_evals), so the config gate, which runs in
the tool, leaves the coder and reviewer cases out. With sandboxes on (VALXEL_SESSION_EXECUTOR=
kubernetes) the gate sends them to one instead, the same disposable sandbox an agent session gets
(gitops/apps/agent-sessions: gVisor, no service account token, egress only to package registries,
GitHub, the model gateway and telemetry), and the runner there replays them under the config at
the pull request's base and at its head:

    tool ──spec: cases, both configs, a read-only token per repo, a gateway token──▶ sandbox
         ◀── both runs' results ── the sandbox is deleted

**What crosses** (`spec_for`): the cases (the coder's task and build context, the merge they are
checked against), the config files of both sides, a contents:read token for each repository the
cases name, and a gateway session token capped at the run's budget for the two configs' coder and
reviewer models, revoked when the run ends. Nothing else of the tool's: no App key, database or
model key reaches the sandbox.

**What comes back**: each side's `evals.run` result. The tool compares them like any other run and
posts `valxel / evals (code)` on the pull request's head.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

from . import session_runner

VERSION = 1
CONTEXT = "valxel / evals (code)"


def enabled(env=None) -> bool:
    """The gate sends code cases out only where they can run safely: a sandbox per run."""
    from .sandbox import remote

    e = os.environ if env is None else env
    return remote() and e.get("VALXEL_EVAL_CODE", "on").lower() not in ("0", "off", "false", "no")


def _config_files(d: str | Path) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(Path(d).glob("*.yaml"))}


def _models(services, data_class: str) -> list[str]:
    """The model ids the coder and reviewer roles (and their fallbacks) may call under this config."""
    out = []
    for role in ("coder", "reviewer"):
        for name in services.roles_for(role, data_class):
            rc = services.settings.roles.get(name)
            if rc is not None:
                out.append(rc.model.split("/", 1)[-1])
    return out


def _repo_tokens(services, cases) -> dict[str, str]:
    from .code_evals import _need

    tokens = getattr(services.github, "tokens", None)
    scoped = getattr(tokens, "scoped", None)
    if not callable(scoped) and any(c.phase in ("coder", "reviewer") for c in cases):
        raise RuntimeError("code evals need the GitHub App, to read the repositories the cases name")
    out = {}
    for c in cases:
        if c.phase not in ("coder", "reviewer"):  # the injection suite makes its own repository
            continue
        repo = _need(c)["repo"]
        if repo not in out:
            org, name = repo.split("/", 1)
            out[repo] = scoped(org, [name], {"contents": "read"})
    return out


def run(services, cases, base_dir, cand_dir, *, budget_usd: float, executor=None, mint=None,
        timeout_s: int = 3600) -> dict:
    """Replay `cases` under both configs in sandboxes, one per data class: {"baseline", "candidate"},
    each {"results", "summary"} as `evals.run` returns them."""
    from . import gateway_sessions, session_runner
    from .evals import services_for, summarize

    executor = executor or session_runner.executor()
    mint = mint or gateway_sessions.mint
    sides = {"baseline": {"results": []}, "candidate": {"results": []}}
    by_class: dict[str, list] = {}
    for c in cases:
        by_class.setdefault(c.data_class, []).append(c)
    base_svc, cand_svc = services_for(services.settings, base_dir), services_for(services.settings, cand_dir)
    for data_class, group in sorted(by_class.items()):
        run_id = uuid.uuid4().hex[:16]
        tags = SimpleNamespace(product_id="valxel", feature_id=f"eval-{run_id}", phase="eval", role="coder",
                               workspace="", data_class=data_class)
        models = list(dict.fromkeys(_models(base_svc, data_class) + _models(cand_svc, data_class)))
        with ExitStack() as stack:
            url, token = stack.enter_context(mint(tags, models + ["claude-haiku-*"], budget_usd * 2, timeout_s + 600))
            spec = {"version": VERSION, "runtime": "eval", "id": f"eval-{run_id}",
                    "cases": [c.as_dict() for c in group],
                    "configs": {"baseline": _config_files(base_dir), "candidate": _config_files(cand_dir)},
                    "repo_tokens": _repo_tokens(services, group),
                    "env": {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_API_KEY": token,
                            **dict.fromkeys(gateway_sessions.CLEARED, "")},
                    "budget_usd": budget_usd, "timeout_s": timeout_s}
            out = executor.run(spec)
        if not out.get("ok"):
            raise RuntimeError(f"the code eval sandbox failed: {out.get('error', 'unknown')}")
        for side, got in sides.items():
            got["results"] += out[side]["results"]
    for side in sides.values():
        side["summary"] = summarize(side["results"])
    return sides


# --------------------------------------------------------------------------- the sandbox's side

class _ReadTokens:
    """What code_evals.checkout asks for a clone: the read-only token the tool sent for that repo."""

    def __init__(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens

    def scoped(self, org: str, names: list[str], permissions: dict) -> str:
        repo = f"{org}/{names[0]}"
        if repo not in self.tokens or permissions != {"contents": "read"}:
            raise PermissionError(f"no read token was sent for {repo}")
        return self.tokens[repo]


def run_spec(spec: dict, root: Path) -> dict:
    """Replay the spec's cases under its two configs, in this disposable sandbox."""
    from .config import load_settings
    from .evals import EvalCase, services_for
    from .evals import run as run_cases

    if spec.get("version") != VERSION or spec.get("runtime") != "eval":
        raise ValueError("unknown eval spec")
    for name in [*spec.get("configs", {}).get("baseline", {}), *spec.get("configs", {}).get("candidate", {})]:
        if "/" in name or name.startswith(".") or not name.endswith(".yaml"):
            raise ValueError(f"not a config file name: {name!r}")
    # This process is the disposable runner: model-written code may run here, sessions run in it
    # (no sandbox inside the sandbox), and models are reached with the run's gateway token only.
    os.environ.update({"VALXEL_EVAL_RUNNER": "1", "VALXEL_SESSION_EXECUTOR": "local", "VALXEL_DELIVERY": "stub",
                       **{k: str(v) for k, v in (spec.get("env") or {}).items()}})
    for k in ("VALXEL_LLM_GATEWAY_URL", "VALXEL_LLM_GATEWAY_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(k, None)
    cases = [EvalCase.from_dict(d) for d in spec.get("cases") or []]
    base = load_settings()
    out = {}
    for side in ("baseline", "candidate"):
        cfg = Path(tempfile.mkdtemp(prefix=f"valxel-{side}-", dir=root))
        shutil.copytree(base.config_dir, cfg, dirs_exist_ok=True)
        for name, text in (spec["configs"].get(side) or {}).items():
            (cfg / name).write_text(text, encoding="utf-8")
        svc = services_for(base, cfg)
        svc.github.tokens = _ReadTokens(dict(spec.get("repo_tokens") or {}))
        out[side] = run_cases(svc, cases, budget_usd=float(spec.get("budget_usd") or 20.0))
    return out


# A sandbox's runner runs this spec too (valxel session-runner); the core runner doesn't import this.
session_runner.register("eval", run_spec, prefix="valxel-eval-")
