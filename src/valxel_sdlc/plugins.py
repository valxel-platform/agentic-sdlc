"""The plugins the agents' skills come from, as config (config/plugins.yaml).

Anthropic's Product Management, Engineering and Design plugins are pinned to one commit of
knowledge-work-plugins. The pin lives in the config, not only in the Dockerfile, so moving it is a
config change like a model or a prompt: a pull request to the config repo, replayed against past
documents (`valxel / evals`) and, if wanted, tried on a share of features first (the canary).

The image builds its plugins from the same pin and records it (`<VALXEL_SKILLS_DIR>/REF`). A config
that pins another commit (an eval run's candidate, or the canary) gets its own copy, fetched once
per commit and laid out by plugins/prepare.sh exactly as the image's is: skills and commands only.

`on_demand` decides whether a role's sessions can also see, and load by themselves, every other
installed skill, beyond the ones its phase pastes into its prompt. Whatever a session does load is
recorded on its ledger rows and trace (`extra_skills`), so a reviewer can see it."""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

SHA = re.compile(r"^[0-9a-f]{40}$")
UPSTREAM = ("product-management", "engineering", "design")
_lock = threading.Lock()


@dataclass(frozen=True)
class PluginsConfig:
    repo: str = "anthropics/knowledge-work-plugins"
    ref: str = ""  # a full commit sha; empty: whatever the image has
    on_demand_default: bool = True
    on_demand_roles: dict[str, bool] = field(default_factory=dict)

    def on_demand(self, role: str) -> bool:
        """Whether this role's sessions see every installed skill, or only their phase's."""
        from .agents.context import base_role

        for name in (role, base_role(role)):
            if name in self.on_demand_roles:
                return self.on_demand_roles[name]
        return self.on_demand_default


def load_plugins(path: Path) -> PluginsConfig:
    if not path.is_file():
        return PluginsConfig()
    raw = yaml.safe_load(path.read_text()) or {}
    kwp = raw.get("knowledge_work_plugins") or {}
    od = raw.get("on_demand") or {}
    cfg = PluginsConfig(
        repo=str(kwp.get("repo", PluginsConfig.repo)),
        ref=str(kwp.get("ref", "") or ""),
        on_demand_default=bool(od.get("default", True)),
        on_demand_roles={str(k): bool(v) for k, v in (od.get("roles") or {}).items()},
    )
    if cfg.ref and not SHA.match(cfg.ref):
        raise ValueError(f"{path.name}: knowledge_work_plugins.ref must be a full commit sha (40 hex characters), "
                         f"not {cfg.ref!r}: a tag or branch can move without a pull request")
    if not re.match(r"^[\w.-]+/[\w.-]+$", cfg.repo):
        raise ValueError(f"{path.name}: knowledge_work_plugins.repo must be owner/name, not {cfg.repo!r}")
    return cfg


def installed_ref(skills_dir: str | None) -> str:
    """The commit the plugins in `skills_dir` were built from ('' when it doesn't say)."""
    p = Path(skills_dir or "") / "REF"
    return p.read_text().strip() if skills_dir and p.is_file() else ""


def plugin_source() -> Path:
    """plugins/ (prepare.sh, marketplace.json and our plugin): in the image, or in a checkout."""
    env = os.environ.get("VALXEL_PLUGIN_SRC")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "plugins"


def _download(repo: str, ref: str) -> bytes:
    url = f"https://codeload.github.com/{repo}/tar.gz/{ref}"
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 (fixed https host)
        return resp.read()


def materialise(cfg: PluginsConfig, *, cache_root: str | Path | None = None, source: Path | None = None,
                fetch=_download) -> str:
    """A plugins folder for `cfg.ref`, laid out like the image's: fetched once, then reused."""
    if not SHA.match(cfg.ref):
        raise ValueError(f"Can't fetch plugins at {cfg.ref!r}: not a commit sha")
    root = Path(cache_root or os.environ.get("VALXEL_PLUGIN_CACHE") or Path(tempfile.gettempdir()) / "valxel-plugins")
    out = root / cfg.ref
    with _lock:
        if installed_ref(str(out)) == cfg.ref:
            return str(out)
        src = source or plugin_source()
        with tempfile.TemporaryDirectory(prefix="valxel-kwp-") as tmp:
            with tarfile.open(fileobj=io.BytesIO(fetch(cfg.repo, cfg.ref)), mode="r:gz") as tar:
                tar.extractall(tmp, filter="data")
            tops = [p for p in Path(tmp).iterdir() if p.is_dir()]
            if len(tops) != 1:
                raise RuntimeError(f"{cfg.repo}@{cfg.ref[:12]}: unexpected archive layout")
            missing = [p for p in UPSTREAM if not (tops[0] / p).is_dir()]
            if missing:
                raise RuntimeError(f"{cfg.repo}@{cfg.ref[:12]} has no {', '.join(missing)} plugin")
            if out.exists():
                shutil.rmtree(out)
            out.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["sh", str(src / "prepare.sh"), str(tops[0]), str(src / "valxel"), str(out)],  # noqa: S603,S607
                           check=True, capture_output=True, text=True)
        (out / "REF").write_text(cfg.ref + "\n")
    log.info("plugins: %s@%s laid out in %s", cfg.repo, cfg.ref[:12], out)
    return str(out)


def skills_dir_for(settings, *, installed: str | None = None, fetch=_download, cache_root=None) -> str:
    """The plugins folder this config's agents use: the image's when the pins agree (or the config
    pins nothing), otherwise a copy at the config's pin."""
    installed = installed if installed is not None else (settings.skills_dir or os.environ.get("VALXEL_SKILLS_DIR", ""))
    want = settings.plugins.ref
    if not want or installed_ref(installed) == want:
        return installed
    return materialise(settings.plugins, cache_root=cache_root, fetch=fetch)


# ------------------------------------------------------------------------- skills a session loaded

_SKILL_PATH = re.compile(r"([^\s\"'`]*?)/skills/([\w.-]+)/SKILL\.md")
_COMMAND_PATH = re.compile(r"([^\s\"'`]*?)/commands/([\w.-]+)\.md")


def skills_in_paths(text: str, plugins: set[str]) -> set[str]:
    """`plugin:skill` for each installed skill or command file `text` mentions (a Codex session's
    events, which show the files it read). The plugin is the nearest folder named like one."""
    found = set()
    for pattern in (_SKILL_PATH, _COMMAND_PATH):
        for m in pattern.finditer(text):
            parts = [p for p in m.group(1).split("/") if p]
            owner = next((p for p in reversed(parts) if p in plugins), "")
            if owner:
                found.add(f"{owner}:{m.group(2)}")
    return found


def extra(loaded, declared) -> tuple[str, ...]:
    """What a session loaded beyond the skills its phase gave it."""
    return tuple(sorted(set(loaded) - set(declared)))
