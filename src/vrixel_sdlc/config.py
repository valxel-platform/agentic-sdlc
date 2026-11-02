"""Tool settings: config/registry.yaml (where each product's governance lives), config/models.yaml
(role registry) and environment variables. Product settings themselves come from each product
org's governance repo; see vrixel_sdlc.governance."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _repo_root() -> Path:
    env = os.environ.get("VRIXEL_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RepoSpec:
    name: str  # "<org>/<repo>"
    role: str  # provider | consumer | library

    @property
    def org(self) -> str:
        return self.name.split("/", 1)[0]


@dataclass(frozen=True)
class ProductConfig:
    """A product's effective settings: registry fields plus its validated governance manifest."""

    product_id: str
    name: str
    org: str
    data_class: str
    workspace: str
    budget_usd: float
    critic_threshold: float
    max_loops: int
    repos: tuple[RepoSpec, ...]
    approvers: dict[str, list[str]]
    high_risk_paths: tuple[str, ...]
    kind: str = "product"  # product (owns a runtime) | shared (ships packages, no runtime)
    runtime: dict | None = None  # {"openchoreo_project", "pipeline"}; None for shared lanes
    depends_on: tuple[str, ...] = ()
    # External tools: {"diagrams": {"provider": "lucid", ...}, "work_tracking": {"provider": "jira", ...}}
    tools: dict = field(default_factory=dict)
    # Effective test and security policy: suite -> spec (see vrixel_sdlc.quality)
    quality: dict = field(default_factory=dict)
    # Effective golden-path settings for new repos (see vrixel_sdlc.provisioning)
    repo_standards: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ServerConfig:
    """A remote MCP server the platform holds credentials for (config/tools.yaml)."""

    name: str
    url: str
    auth_header: str = "Authorization"
    auth_env: str = ""
    settings: dict = field(default_factory=dict)

    def auth_value(self) -> str:
        return os.environ.get(self.auth_env, "") if self.auth_env else ""

    def setting(self, key: str) -> str:
        """A server setting, read from the environment variable named by `<key>_env`."""
        env = self.settings.get(f"{key}_env")
        return os.environ.get(env, "") if env else str(self.settings.get(key, ""))


@dataclass(frozen=True)
class ToolsConfig:
    servers: dict[str, ServerConfig] = field(default_factory=dict)
    providers: dict[str, dict[str, dict]] = field(default_factory=dict)  # capability -> provider -> spec
    data_classes: dict[str, list[str]] = field(default_factory=dict)  # data class -> allowed providers

    def allowed(self, data_class: str, provider: str) -> bool:
        return provider in self.data_classes.get(data_class, [])

    def server_for(self, capability: str, provider: str) -> ServerConfig | None:
        name = (self.providers.get(capability, {}).get(provider) or {}).get("server")
        return self.servers.get(name) if name else None


def load_tools(path: Path) -> ToolsConfig:
    if not path.exists():
        return ToolsConfig()
    raw = yaml.safe_load(path.read_text()) or {}
    servers = {}
    for name, spec in (raw.get("servers") or {}).items():
        url = (os.environ.get(spec["url_env"], "") if spec.get("url_env") else "") or spec.get("url", "")
        servers[name] = ServerConfig(name=name, url=url, auth_header=spec.get("auth_header", "Authorization"),
                                     auth_env=spec.get("auth_env", ""), settings=dict(spec.get("settings") or {}))
    providers = {cap: {p: dict(v or {}) for p, v in (provs or {}).items()}
                 for cap, provs in (raw.get("providers") or {}).items()}
    return ToolsConfig(servers=servers, providers=providers,
                       data_classes={k: list(v or []) for k, v in (raw.get("data_classes") or {}).items()})


@dataclass(frozen=True)
class RepoPointer:
    org: str
    repo: str
    ref: str = "main"

    @property
    def full_name(self) -> str:
        return f"{self.org}/{self.repo}"


@dataclass(frozen=True)
class RegistryEntry:
    product_id: str
    governance: RepoPointer
    data_class: str
    workspace: str
    kind: str = "product"


@dataclass(frozen=True)
class Limits:
    min_critic_threshold: float = 0.75
    max_loops: int = 5
    max_budget_usd: float = 500.0
    required_gates: tuple[str, ...] = ("g1", "g2", "g3", "g4", "g5", "g6")


@dataclass(frozen=True)
class RoleConfig:
    runtime: str  # claude_code | generic
    model: str  # "<provider>/<model-name>"
    ttl: str = "1h"

    @property
    def provider(self) -> str:
        return self.model.split("/", 1)[0]

    @property
    def model_name(self) -> str:
        return self.model.split("/", 1)[-1]


@dataclass
class Settings:
    mode: str  # stub | live
    database_url: str | None
    artifact_dir: Path
    config_dir: Path
    governance_source: str  # local | github
    governance_root: Path
    github_api: str
    platform: RepoPointer
    platform_constitution_path: str
    limits: Limits
    registry: dict[str, RegistryEntry]
    roles: dict[str, RoleConfig]
    data_classes: dict[str, list[str]]
    failover: dict[str, list[str]] = field(default_factory=dict)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    quality: Any = None  # vrixel_sdlc.quality.QualityConfig
    repos: Any = None  # vrixel_sdlc.provisioning.RepoStandards

    def entry(self, product_id: str) -> RegistryEntry:
        try:
            return self.registry[product_id]
        except KeyError as exc:
            raise KeyError(f"Unknown product {product_id!r}; registered: {sorted(self.registry)}") from exc

    def role(self, role: str) -> RoleConfig:
        return self.roles[role]


def load_settings(
    *,
    mode: str | None = None,
    database_url: str | None = None,
    artifact_dir: str | Path | None = None,
    config_dir: str | Path | None = None,
    governance_source: str | None = None,
    governance_root: str | Path | None = None,
) -> Settings:
    root = _repo_root()
    cfg_dir = Path(config_dir or os.environ.get("VRIXEL_CONFIG_DIR") or root / "config")
    art_dir = Path(artifact_dir or os.environ.get("VRIXEL_ARTIFACT_DIR") or root / ".artifacts")

    reg = yaml.safe_load((cfg_dir / "registry.yaml").read_text())
    models_raw = yaml.safe_load((cfg_dir / "models.yaml").read_text())
    gov = reg.get("governance", {})
    gov_root = Path(governance_root or os.environ.get("VRIXEL_GOVERNANCE_ROOT") or gov.get("local_root", "examples/governance"))
    if not gov_root.is_absolute():
        gov_root = root / gov_root

    plat = reg["platform"]
    lim = reg.get("limits", {})
    registry = {
        pid: RegistryEntry(
            product_id=pid,
            governance=RepoPointer(org=p["org"], repo=p.get("repo", "governance"), ref=p.get("ref", "main")),
            data_class=p["data_class"],
            workspace=p["workspace"],
            kind=p.get("kind", "product"),
        )
        for pid, p in reg["products"].items()
    }
    return Settings(
        mode=(mode or os.environ.get("VRIXEL_MODE") or "stub").lower(),
        database_url=database_url if database_url is not None else os.environ.get("DATABASE_URL"),
        artifact_dir=art_dir,
        config_dir=cfg_dir,
        governance_source=(governance_source or os.environ.get("VRIXEL_GOVERNANCE_SOURCE") or gov.get("source", "local")).lower(),
        governance_root=gov_root,
        github_api=os.environ.get("VRIXEL_GITHUB_API") or gov.get("github_api", "https://api.github.com"),
        platform=RepoPointer(org=plat["org"], repo=plat["repo"], ref=plat.get("ref", "main")),
        platform_constitution_path=plat.get("constitution_path", "constitution.md"),
        limits=Limits(
            min_critic_threshold=float(lim.get("min_critic_threshold", 0.75)),
            max_loops=int(lim.get("max_loops", 5)),
            max_budget_usd=float(lim.get("max_budget_usd", 500)),
            required_gates=tuple(lim.get("required_gates", Limits.required_gates)),
        ),
        registry=registry,
        roles={name: RoleConfig(**spec) for name, spec in models_raw["roles"].items()},
        data_classes={n: list(s["allowed_providers"]) for n, s in models_raw["data_classes"].items()},
        failover=models_raw.get("failover") or {},
        tools=load_tools(cfg_dir / "tools.yaml"),
        quality=_load_quality(cfg_dir / "quality.yaml"),
        repos=_load_repos(cfg_dir / "repos.yaml"),
    )


def _load_repos(path: Path):
    from .provisioning import load_standards

    return load_standards(path)


def _load_quality(path: Path):
    from .quality import load_quality

    return load_quality(path)
