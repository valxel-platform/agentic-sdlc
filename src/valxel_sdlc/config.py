"""Tool settings: config/registry.yaml (where each product's governance lives), config/models.yaml
(role registry) and environment variables. Product settings themselves come from each product
org's governance repo; see valxel_sdlc.governance."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _repo_root() -> Path:
    env = os.environ.get("VALXEL_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RepoSpec:
    name: str  # "<org>/<repo>"
    role: str  # provider | consumer | library
    # A shared lane's library: what its release workflow publishes to GitHub Packages, so G6 can
    # check a release candidate exists: {"type": maven|npm|nuget|rubygems|container, "name": ...}.
    package: dict | None = field(default=None, hash=False, compare=False)

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
    # Percent of the feature budget each group of phases may spend (valxel_sdlc.budgets); None: defaults.
    budget_shares: dict | None = None
    runtime: dict | None = None  # {"openchoreo_project", "pipeline"}; None for shared lanes
    depends_on: tuple[str, ...] = ()
    # External tools: {"diagrams": {"provider": "lucid", ...}, "work_tracking": {"provider": "jira", ...}}
    tools: dict = field(default_factory=dict)
    # Effective test and security policy: suite -> spec (see valxel_sdlc.quality)
    quality: dict = field(default_factory=dict)
    # Performance budgets and rules over the industry defaults (valxel_sdlc.performance.settings)
    performance: dict = field(default_factory=dict)
    # Which workflow profiles people may start work with, and the default (valxel_sdlc.profiles)
    workflow: dict = field(default_factory=dict)
    # Effective golden-path settings for new repos (see valxel_sdlc.provisioning)
    repo_standards: dict = field(default_factory=dict)
    # Name prefix for new repos (e.g. 'cln-'). Required while a product has no repos yet.
    repo_prefix: str = ""
    # What happens to security findings on the default branch after merge (valxel_sdlc.upkeep.effective)
    upkeep: dict = field(default_factory=dict)


def _secret(name: str) -> str:
    from .secret_ring import get

    return get(name)


@dataclass(frozen=True)
class ServerConfig:
    """A remote MCP server the platform holds credentials for (config/tools.yaml)."""

    name: str
    url: str
    auth_header: str = "Authorization"
    auth_env: str = ""
    settings: dict = field(default_factory=dict)

    def auth_value(self) -> str:
        from .secret_ring import get

        return get(self.auth_env) if self.auth_env else ""

    def setting(self, key: str) -> str:
        """A server setting, read from the environment variable named by `<key>_env` (a secret,
        such as `client_secret`, through the secret store)."""
        from .secret_ring import environ, get

        env = self.settings.get(f"{key}_env")
        if not env:
            return str(self.settings.get(key, ""))
        return get(env) if ("secret" in key or ("token" in key and "url" not in key)) else environ().get(env, "")


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
    max_initiative_budget_usd: float = 5000.0
    required_gates: tuple[str, ...] = ("g1", "g2", "g3", "g4", "g5", "g6")
    # A gate sent back this many times suggests taking the phase over; at max_gate_revisions it
    # can't be sent back again: a person takes it over (edits the document and hands it back) or stops it.
    takeover_after_revisions: int = 2
    max_gate_revisions: int = 3
    # Separation of duties (valxel_sdlc.separation): the gates where whoever wrote the work can't
    # approve it, and the data classes where that holds at every gate.
    separation_of_duties_gates: tuple[str, ...] = ("g5", "g6")
    separation_of_duties_data_classes: tuple[str, ...] = ("phi", "payments")
    # A team with one approver (a solo founder): the author may approve their own work where the rule
    # applies, after saying why no one else can. Recorded as "waived", not "checked", and listed for
    # review (GET /api/waivers). Off: the author can't approve; the rule has no silent off switch.
    separation_of_duties_waiver: bool = False


@dataclass(frozen=True)
class RoleConfig:
    runtime: str  # claude_code | codex | generic
    model: str  # "<provider>/<model-name>"
    ttl: str = "1h"
    # Caps for one agent session: turns, and dollars (also never more than what's left of the
    # feature's budget). A session that hits one stops and its phase sees the failure. Codex has
    # no turn or dollar cap of its own: it gets max_minutes, and its cost is checked after.
    max_turns: int = 40
    max_call_usd: float = 10.0
    max_minutes: int = 30
    # Other roles to run the same task when this one's provider is unavailable (rate limited,
    # overloaded, down), tried in order; each must be allowed the product's data class.
    fallback: tuple[str, ...] = ()
    # Other roles (ideally other model families) that may review the same change too: the first
    # one that can run and may see the product's data class does.
    second_opinion: tuple[str, ...] = ()
    # True: the second opinion's blocking findings block too. False: they are reported only.
    second_opinion_blocking: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "fallback", tuple(self.fallback or ()))
        so = self.second_opinion
        object.__setattr__(self, "second_opinion", (so,) if isinstance(so, str) else tuple(so or ()))

    @property
    def provider(self) -> str:
        return self.model.split("/", 1)[0]

    @property
    def model_name(self) -> str:
        return self.model.split("/", 1)[-1]


@dataclass(frozen=True)
class ProviderConfig:
    """A model provider reached over an OpenAI-compatible API (OpenAI, a self-hosted vLLM, the
    products' LLM gateway). Anthropic needs no entry: it is built in."""

    name: str
    base_url: str = ""
    base_url_env: str = ""  # read the URL from this variable instead (self-hosted endpoints)
    api_key_env: str = ""
    # What a self-hosted server runs (a weights revision or image digest): it serves whatever it was
    # started with, so this is how its model is pinned (valxel_sdlc.pinning).
    pinned: str = ""

    def url(self) -> str:
        return (os.environ.get(self.base_url_env, "") if self.base_url_env else self.base_url).rstrip("/")

    def api_key(self) -> str:
        from .secret_ring import get

        return get(self.api_key_env) if self.api_key_env else ""

    def configured(self) -> bool:
        return bool(self.url()) and (not self.api_key_env or bool(self.api_key()))


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
    platform_resources_path: str
    limits: Limits
    registry: dict[str, RegistryEntry]
    roles: dict[str, RoleConfig]
    data_classes: dict[str, list[str]]
    providers: dict[str, "ProviderConfig"] = field(default_factory=dict)
    prices: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    # Each model's context window in tokens (models.yaml `context_windows`, from the vendor's docs),
    # by "provider/model" or the bare model name.
    context_windows: dict[str, int] = field(default_factory=dict)
    # Token caps per prompt layer (models.yaml `context_caps`): a layer over its cap is cut at a
    # heading, with a note naming what was left out (gateway.prompt.fit). `reserve` is the room a
    # role's model must have left for the task, its tool results and the reply.
    context_caps: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_CONTEXT_CAPS))
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    quality: Any = None  # valxel_sdlc.quality.QualityConfig
    repos: Any = None  # valxel_sdlc.provisioning.RepoStandards
    knowledge: Any = None  # valxel_sdlc.knowledge.KnowledgeConfig
    plugins: Any = None  # valxel_sdlc.plugins.PluginsConfig
    # The installed plugins the agents' skills come from (VALXEL_SKILLS_DIR, or a copy at this config's pin).
    skills_dir: str = ""
    # Product inception: {starters: [...], council: [...], budget_usd} (registry.yaml `inception`).
    inception: dict = field(default_factory=dict)
    # The platform's default alert rules; unset, alert-rules.yaml next to the AWS catalog.
    platform_alert_rules_path: str = ""
    # Tiers, headroom and starting throughput for capacity plans; unset, capacity.yaml next to the AWS catalog.
    platform_capacity_path: str = ""
    # Where error reports may go; unset, error-tracking.yaml next to the AWS catalog.
    platform_error_tracking_path: str = ""
    # What the tool may do in production by itself; unset, remediation.yaml next to the AWS catalog.
    platform_remediation_path: str = ""

    def __post_init__(self) -> None:
        folder = self.platform_resources_path.rsplit("/", 1)[0] if "/" in self.platform_resources_path else ""
        if not self.platform_alert_rules_path:
            self.platform_alert_rules_path = f"{folder}/alert-rules.yaml" if folder else "alert-rules.yaml"
        if not self.platform_capacity_path:
            self.platform_capacity_path = f"{folder}/capacity.yaml" if folder else "capacity.yaml"
        if not self.platform_error_tracking_path:
            self.platform_error_tracking_path = f"{folder}/error-tracking.yaml" if folder else "error-tracking.yaml"
        if not self.platform_remediation_path:
            self.platform_remediation_path = f"{folder}/remediation.yaml" if folder else "remediation.yaml"

    def entry(self, product_id: str) -> RegistryEntry:
        try:
            return self.registry[product_id]
        except KeyError as exc:
            raise KeyError(f"Unknown product {product_id!r}; registered: {sorted(self.registry)}") from exc

    def role(self, role: str) -> RoleConfig:
        return self.roles[role]

    def context_window(self, model: str) -> int:
        """The model's context window in tokens, 0 when config doesn't say."""
        from .pinning import undated

        w = self.context_windows
        for name in dict.fromkeys((model, undated(model))):  # a pinned snapshot has its model's window
            bare = name.split("/", 1)[-1]
            found = w.get(name) or w.get(bare) or next((v for k, v in w.items() if k.split("/", 1)[-1] == bare), 0)
            if found:
                return int(found)
        return 0


DEFAULT_CONTEXT_CAPS = {"L2": 15000, "L3": 8000, "L4": 10000, "L5": 25000, "reserve": 64000}


def config_file(cfg_dir: Path | str, name: str) -> Path:
    """A config file from the deployment's directory (VALXEL_CONFIG_DIR), or, when the deployment
    doesn't override it, the tool's own default. A deployment keeps only what it changes (its
    registry, its models), so it never carries a stale copy of the golden path or the contract."""
    path = Path(cfg_dir) / name
    return path if path.exists() else _repo_root() / "config" / name


def load_settings(
    *,
    mode: str | None = None,
    database_url: str | None = None,
    artifact_dir: str | Path | None = None,
    config_dir: str | Path | None = None,
    governance_source: str | None = None,
    governance_root: str | Path | None = None,
) -> Settings:
    from .secret_ring import adopt_settings

    moved = adopt_settings()
    if moved:
        logging.getLogger(__name__).warning(
            "settings kept with the secrets, read from VALXEL_SECRETS_DIR: %s; set them in the environment instead",
            ", ".join(moved))
    root = _repo_root()
    cfg_dir = Path(config_dir or os.environ.get("VALXEL_CONFIG_DIR") or root / "config")
    art_dir = Path(artifact_dir or os.environ.get("VALXEL_ARTIFACT_DIR") or root / ".artifacts")

    reg = yaml.safe_load((cfg_dir / "registry.yaml").read_text())
    models_raw = yaml.safe_load(config_file(cfg_dir, "models.yaml").read_text())
    gov = reg.get("governance", {})
    gov_root = Path(governance_root or os.environ.get("VALXEL_GOVERNANCE_ROOT") or gov.get("local_root", "examples/governance"))
    if not gov_root.is_absolute():
        gov_root = root / gov_root

    plat = reg["platform"]
    lim = reg.get("limits", {})
    registry = {
        pid: RegistryEntry(
            product_id=pid,
            governance=RepoPointer(org=p["org"], repo=p.get("repo", f"{pid.lower()}-governance"), ref=p.get("ref", "main")),
            data_class=p["data_class"],
            workspace=p["workspace"],
            kind=p.get("kind", "product"),
        )
        for pid, p in reg["products"].items()
    }
    settings = Settings(
        mode=(mode or os.environ.get("VALXEL_MODE") or "stub").lower(),
        database_url=database_url if database_url is not None else (_secret("DATABASE_URL") or None),
        artifact_dir=art_dir,
        config_dir=cfg_dir,
        governance_source=(governance_source or os.environ.get("VALXEL_GOVERNANCE_SOURCE") or gov.get("source", "local")).lower(),
        governance_root=gov_root,
        github_api=os.environ.get("VALXEL_GITHUB_API") or gov.get("github_api", "https://api.github.com"),
        platform=RepoPointer(org=plat["org"], repo=plat["repo"], ref=plat.get("ref", "main")),
        platform_constitution_path=plat.get("constitution_path", "constitution.md"),
        platform_resources_path=plat.get("resources_path", "aws-resources.yaml"),
        platform_alert_rules_path=plat.get("alert_rules_path", ""),
        platform_capacity_path=plat.get("capacity_path", ""),
        platform_error_tracking_path=plat.get("error_tracking_path", ""),
        platform_remediation_path=plat.get("remediation_path", ""),
        limits=Limits(
            min_critic_threshold=float(lim.get("min_critic_threshold", 0.75)),
            max_loops=int(lim.get("max_loops", 5)),
            max_budget_usd=float(lim.get("max_budget_usd", 500)),
            max_initiative_budget_usd=float(lim.get("max_initiative_budget_usd", 5000)),
            required_gates=tuple(lim.get("required_gates", Limits.required_gates)),
            takeover_after_revisions=int(lim.get("takeover_after_revisions", 2)),
            max_gate_revisions=int(lim.get("max_gate_revisions", 3)),
            separation_of_duties_gates=tuple(lim.get("separation_of_duties_gates", Limits.separation_of_duties_gates)),
            separation_of_duties_data_classes=tuple(
                lim.get("separation_of_duties_data_classes", Limits.separation_of_duties_data_classes)),
            separation_of_duties_waiver=bool(lim.get("separation_of_duties_waiver", False)),
        ),
        registry=registry,
        roles={name: RoleConfig(**spec) for name, spec in models_raw["roles"].items()},
        data_classes={n: list(s["allowed_providers"]) for n, s in models_raw["data_classes"].items()},
        providers={n: ProviderConfig(name=n, **(p or {})) for n, p in (models_raw.get("providers") or {}).items()},
        prices={m: tuple(float(x) for x in v) for m, v in (models_raw.get("prices") or {}).items()},
        context_windows={m: int(v) for m, v in (models_raw.get("context_windows") or {}).items()},
        context_caps={**DEFAULT_CONTEXT_CAPS, **{k: int(v) for k, v in (models_raw.get("context_caps") or {}).items()}},
        tools=load_tools(config_file(cfg_dir, "tools.yaml")),
        quality=_load_quality(config_file(cfg_dir, "quality.yaml")),
        knowledge=_load_knowledge(config_file(cfg_dir, "knowledge.yaml")),
        repos=_load_repos(config_file(cfg_dir, "repos.yaml")),
        plugins=_load_plugins(config_file(cfg_dir, "plugins.yaml")),
        skills_dir=os.environ.get("VALXEL_SKILLS_DIR", ""),
        inception=dict(reg.get("inception") or {}),
    )
    from .models import validate

    validate(settings)
    return settings


def _load_repos(path: Path):
    from .provisioning import load_standards

    return load_standards(path)


def _load_plugins(path: Path):
    from .plugins import load_plugins

    return load_plugins(path)


def _load_knowledge(path: Path):
    from .knowledge import load_knowledge

    return load_knowledge(path)


def _load_quality(path: Path):
    from .quality import load_quality

    return load_quality(path)
