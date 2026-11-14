"""Service container wired into graph nodes: gateway, ledger, runtimes, stores, integrations."""

from __future__ import annotations

from dataclasses import dataclass

from .artifacts import ArtifactStore
from .config import Settings, load_settings
from .gateway import InMemoryLedger, ModelGateway, StubProvider
from .governance import GovernanceLoader, GovernanceSnapshot, GovernanceSource, build_source
from .gateway.ledger import Ledger
from .integrations import (
    CapabilityCatalog,
    GitHub,
    OpenChoreo,
    PackageRegistry,
    StubGitHub,
    StubOpenChoreo,
    StubPackageRegistry,
    default_catalog,
)
from .integrations.diagrams import DiagramPublisher, DrawioPublisher, LucidPublisher, StubLucidPublisher
from .integrations.mcp_client import ClientCredentials, McpHttp
from .integrations.worktracking import JiraTracker, OpenProjectTracker, StubTracker, WorkTracker
from .runtime import AgentRuntime, ClaudeCodeRuntime, CodexRuntime, GatewayRuntime


@dataclass
class AgentStack:
    """A config's agents: its settings, gateway (tagging each call with its config version), runtimes
    and the roles it can run. The canary is a second stack that a share of new features uses."""

    settings: Settings
    gateway: ModelGateway
    runtimes: dict
    available_roles: frozenset
    percent: int = 0
    data_classes: tuple = ("general",)
    config_dir: str = ""


@dataclass
class Services:
    settings: Settings
    ledger: Ledger
    gateway: ModelGateway
    artifacts: ArtifactStore
    github: GitHub
    openchoreo: OpenChoreo
    catalog: CapabilityCatalog
    runtimes: dict[str, AgentRuntime]
    governance: GovernanceLoader
    packages: PackageRegistry
    diagrams: dict[str, DiagramPublisher]  # provider -> publisher
    trackers: dict[str, WorkTracker]  # provider -> tracker
    quality: object = None  # valxel_sdlc.quality.Quality, set by build_services
    provisioner: object = None  # valxel_sdlc.provisioning.RepoProvisioner, set by build_services
    knowledge: object = None  # valxel_sdlc.knowledge.Knowledge, set by build_services
    features: object = None  # product_id -> feature summaries, set by the engine
    initiatives: object = None  # product_id -> initiatives the engine is running, set by the engine
    feature_titles: object = None  # feature ids -> titles, set by the engine
    release_waits: object = None  # resolves release waits (feature trains, release readiness), set by the engine
    release_for: object = None  # feature_id -> the release it belongs to, set by the engine
    roadmap_fit: object = None  # feature state -> its initiative and budget, set by the engine
    flags: object = None  # valxel_sdlc.integrations.flags.FlagService
    signals: object = None  # valxel_sdlc.ops.signals.SignalStore
    incident_signals: object = None  # incident id -> its signals, set by the engine
    suspects: object = None  # (product, repo) -> released features that changed it, set by the engine
    feature_state: object = None  # feature id -> its state, set by the engine
    flag_of: object = None  # feature id -> its flag key, set by the engine
    start_hotfix: object = None  # starts an expedited fix feature, set by the engine
    start_intent_change: object = None  # starts a feature or initiative from an incident, set by the engine
    close_incident: object = None  # set by the engine
    incidents: object = None  # product_id -> recent incidents, set by the engine
    costs: object = None  # valxel_sdlc.ops.cost.CostSource: what released features really cost
    cost_reviews: object = None  # valxel_sdlc.ops.cost reviews store
    cost_ledger: object = None  # valxel_sdlc.finops.ledger.CostLedger
    alarms: object = None  # valxel_sdlc.ops.alarms board: every CloudWatch alarm's current state
    pager: object = None  # valxel_sdlc.ops.paging.Pager: pages on-call when an incident opens
    changes: object = None  # valxel_sdlc.ops.changes.ChangeLog: every production change, hash-chained
    inventory_snapshots: object = None  # valxel_sdlc.ops.inventory snapshots: counts per day, for the trend
    aws_tags: object = None  # valxel_sdlc.ops.inventory.AwsTags, when VALXEL_AWS_INVENTORY_ROLE is set
    agents: object = None  # valxel_sdlc.integrations.openchoreo_agents: OpenChoreo's SRE and FinOps agents
    change_receipts: object = None  # valxel_sdlc.ops.servicenow receipts store: ServiceNow numbers per change
    effort: object = None  # valxel_sdlc.pilot.EffortStore: people's effort the pilot report counts
    lessons: object = None  # valxel_sdlc.lessons store: people's corrections, and the lessons proposed from them
    remediation_record: object = None  # (product, window_days) -> the tool's own fixes' track record, set by the engine
    feature_info: object = None  # feature id -> {risk_tier, components}, set by the engine
    cohort_logs: object = None  # valxel_sdlc.ops.cohorts log source: users with a flag on against off
    readings: object = None  # valxel_sdlc.outcomes.store: goal metric readings, taken and typed in
    metric_sources: object = None  # provider -> valxel_sdlc.outcomes.sources.MetricSource

    available_roles: frozenset = frozenset()  # roles whose provider is configured, set by build_services
    telemetry: object = None  # valxel_sdlc.telemetry.Telemetry: each agent session as a span, set by build_services
    attachments: object = None  # valxel_sdlc.attachments.AttachmentStore: files attached at intake
    evals: object = None  # valxel_sdlc.evals store: eval cases from finished document phases, labelled at the gates
    canary: object = None  # AgentStack: a candidate config a share of new features runs with (VALXEL_CANARY_*)

    def _stack(self, variant: str = ""):
        return self.canary if variant == "canary" and self.canary is not None else self

    def canary_variant(self, feature_id: str, data_class: str, track: str = "standard") -> str:
        """"canary" for the share of new features the candidate config runs; "" for the rest."""
        import hashlib

        c = self.canary
        if c is None or not c.percent or track == "hotfix" or data_class not in c.data_classes:
            return ""
        return "canary" if int(hashlib.sha1(feature_id.encode()).hexdigest(), 16) % 100 < c.percent else ""

    def runtime_for(self, role: str, variant: str = "") -> AgentRuntime:
        stack = self._stack(variant)
        kind = stack.settings.role(role).runtime
        runtime = stack.runtimes.get(kind) or stack.runtimes["generic"]
        if self.telemetry is not None and self.telemetry.enabled:
            from .telemetry import TracedRuntime

            return TracedRuntime(runtime, self.telemetry)
        return runtime

    def roles_for(self, role: str, data_class: str, variant: str = "") -> list[str]:
        """The role, then its fallbacks for when its provider is unavailable."""
        from .models import chain

        stack = self._stack(variant)
        return chain(stack.settings, set(stack.available_roles or stack.settings.roles), role, data_class)

    def second_opinion(self, role: str, data_class: str, reviewed_by: str | None = None):
        """Another model to review the same work, if the role has one that can run here."""
        from .models import second_opinion

        return second_opinion(self.settings, set(self.available_roles or self.settings.roles), role, data_class,
                              reviewed_by)

    def snapshot(self, product_id: str, pins: dict | None = None) -> GovernanceSnapshot:
        """The product's governance, read from its own org. Pass the feature's pins inside a
        feature thread; omit them to resolve the current commit (intake and admin views)."""
        return self.governance.snapshot(product_id, pins)

    def feature_snapshot(self, state: dict) -> GovernanceSnapshot:
        """The feature's pinned governance, plus repos provisioned for it. Until the governance PR
        that registers them merges, the feature carries them itself, as repos of its own product."""
        from dataclasses import replace

        from .config import RepoSpec

        snap = self.snapshot(state["product_id"], state.get("governance"))
        known = {r.name for r in snap.product.repos}
        extra = tuple(RepoSpec(name=r["repo"], role=r["role"]) for r in state.get("new_repos") or []
                      if r.get("status") == "provisioned" and r["repo"] not in known)
        if not extra:
            return snap
        return replace(snap, product=replace(snap.product, repos=snap.product.repos + extra))


def check_skills(base: str | None = None) -> None:
    """Live agents work from the plugins' skills; a missing one would reach them as a placeholder.
    Refuse to start instead, naming what is missing."""
    import os

    from .agents.context import missing_skills
    from .agents.roles import used_skills

    base = base if base is not None else os.environ.get("VALXEL_SKILLS_DIR")
    if not base:
        raise RuntimeError("Live mode needs VALXEL_SKILLS_DIR: the folder of installed plugins (the image sets it)")
    missing = missing_skills(used_skills(), base)
    if missing:
        raise RuntimeError(f"Skills missing from {base}: {', '.join(missing)}")


def use_own_plugins(settings: Settings) -> None:
    """A config other than the one in use (an eval run's, the canary's) gets the plugins it pins:
    the image's when the pins agree, otherwise a copy at its commit (valxel_sdlc.plugins)."""
    if settings.mode != "live" or settings.plugins is None:
        return
    from .plugins import skills_dir_for

    settings.skills_dir = skills_dir_for(settings)


def agent_stack(settings: Settings, ledger: Ledger):
    """The gateway, runtimes and runnable roles for one config."""
    from .models import available_roles

    available = available_roles(settings)
    providers: dict = {"stub": StubProvider()}
    if settings.mode == "live":
        from .gateway import AnthropicProvider, OpenAICompatibleProvider

        if any(settings.role(r).provider == "anthropic" for r in available):
            providers["anthropic"] = AnthropicProvider()
        for name, pc in settings.providers.items():
            if pc.configured():
                providers[name] = OpenAICompatibleProvider(name, pc.url(), pc.api_key())
    gateway = ModelGateway(settings, ledger, providers)
    generic = GatewayRuntime(gateway)
    runtimes: dict[str, AgentRuntime] = {"generic": generic}
    if settings.mode == "live":
        check_skills(settings.skills_dir or None)
        runtimes["claude_code"] = ClaudeCodeRuntime(gateway)
        runtimes["codex"] = CodexRuntime(gateway)
    else:
        runtimes["claude_code"] = GatewayRuntime(
            gateway, name="claude_code", capabilities=frozenset({"text", "native_plugins", "subagents"})
        )
        runtimes["codex"] = GatewayRuntime(gateway, name="codex", capabilities=frozenset({"text", "code_execution"}))
    return gateway, runtimes, available


def default_canary(settings: Settings, ledger: Ledger, env=None):
    """A second config a share of new features runs with: VALXEL_CANARY_CONFIG_DIR (a config
    directory like config/), VALXEL_CANARY_PERCENT (1-50) and VALXEL_CANARY_DATA_CLASSES (general by
    default). Its roles must cover every role the config in use has."""
    import os

    env = env if env is not None else os.environ
    path, percent = env.get("VALXEL_CANARY_CONFIG_DIR", ""), int(env.get("VALXEL_CANARY_PERCENT", "0") or 0)
    if not path or percent <= 0:
        return None
    if percent > 50:
        raise ValueError("VALXEL_CANARY_PERCENT is at most 50: a canary is the smaller share")
    canary = load_settings(mode=settings.mode, database_url="", config_dir=path, artifact_dir=settings.artifact_dir,
                           governance_source=settings.governance_source, governance_root=settings.governance_root)
    missing = sorted(set(settings.roles) - set(canary.roles))
    if missing:
        raise ValueError(f"The canary config at {path} lacks roles {', '.join(missing)}")
    use_own_plugins(canary)
    gateway, runtimes, available = agent_stack(canary, ledger)
    classes = tuple(c for c in env.get("VALXEL_CANARY_DATA_CLASSES", "general").split(",") if c)
    return AgentStack(settings=canary, gateway=gateway, runtimes=runtimes, available_roles=frozenset(available),
                      percent=percent, data_classes=classes, config_dir=str(path))


def build_services(
    settings: Settings | None = None,
    *,
    ledger: Ledger | None = None,
    github: GitHub | None = None,
    openchoreo: OpenChoreo | None = None,
    catalog: CapabilityCatalog | None = None,
    governance_source: GovernanceSource | None = None,
    packages: PackageRegistry | None = None,
    diagrams: dict[str, DiagramPublisher] | None = None,
    trackers: dict[str, WorkTracker] | None = None,
) -> Services:
    settings = settings or load_settings()
    ledger = ledger or InMemoryLedger()
    gateway, runtimes, available = agent_stack(settings, ledger)
    if github is None or openchoreo is None:
        live_github, live_oc = default_delivery(settings)
        github, openchoreo = github or live_github, openchoreo or live_oc
    svc = Services(
        settings=settings,
        ledger=ledger,
        gateway=gateway,
        artifacts=ArtifactStore(settings.artifact_dir),
        github=github,
        openchoreo=openchoreo,
        catalog=catalog or default_catalog(),
        runtimes=runtimes,
        governance=GovernanceLoader(settings, governance_source or build_source(settings)),
        packages=packages or default_packages(github),
        diagrams=diagrams if diagrams is not None else default_diagrams(settings, github),
        trackers=trackers if trackers is not None else default_trackers(settings),
    )
    svc.quality = default_quality(svc)
    svc.knowledge = default_knowledge(svc)
    svc.flags = default_flags(settings, svc)
    svc.provisioner = default_provisioner(settings, github, openchoreo)
    # Lite mode (valxel_sdlc.lite): live lite products deploy through their VM runtime, everything
    # else through OpenChoreo. On for instances that make lite products, or when asked.
    import os as _os

    from .environments import default_mode

    if default_mode() == "lite" or _os.environ.get("VALXEL_LITE_ROUTING", "").lower() in ("1", "true", "on"):
        from .lite.wiring import RuntimeRouter

        svc.openchoreo = RuntimeRouter(openchoreo, svc)
    svc.available_roles = frozenset(available)
    svc.telemetry = default_telemetry(settings)
    from .attachments import AttachmentStore

    svc.attachments = AttachmentStore(settings.artifact_dir)
    from .notify import Notifier

    svc.notifier = Notifier(settings.artifact_dir)
    svc.canary = default_canary(settings, ledger)
    from .evals import InMemoryEvalStore

    svc.evals = InMemoryEvalStore()
    return svc


def default_telemetry(settings: Settings):
    """Agent sessions as OpenTelemetry spans when VALXEL_OTEL_ENDPOINT is set (config/telemetry.yaml)."""
    from .pilot import config_version
    from .telemetry import Telemetry, load_config

    return Telemetry(load_config(settings.config_dir), config_version=config_version(settings))


def default_packages(github):
    """Live delivery tags shared-lane releases on GitHub and checks GitHub Packages; else in memory."""
    import os

    if os.environ.get("VALXEL_DELIVERY", "stub").lower() == "live":
        from .integrations.packages import GitHubPackages

        return GitHubPackages(github)
    return StubPackageRegistry()


def default_flags(settings: Settings, svc: Services):
    """Each product's own Flipt, from its product.yaml; see integrations/flags.py."""
    import os

    from .integrations.flags import ProductFlags

    def product_flags(product_id: str):
        try:
            product = svc.snapshot(product_id).product
        except Exception:
            return None
        return (product.tools or {}).get("flags") or {}, product.runtime or {}

    spec = (settings.tools.providers.get("flags", {}).get("flipt") or {})
    from .secret_ring import environ

    return ProductFlags(product_flags, live=os.environ.get("VALXEL_DELIVERY", "stub").lower() == "live", env=environ(),
                        token_prefix=spec.get("token_env_prefix", "VALXEL_FLIPT_TOKEN_"))


def default_provisioner(settings: Settings, github, openchoreo):
    """Live delivery creates repos with its own, broader GitHub App token (administration:write),
    used only by the provision step; stubs share the in-memory GitHub."""
    import os

    from .provisioning import RepoProvisioner

    if os.environ.get("VALXEL_DELIVERY", "stub").lower() == "live":
        from .integrations.github import PROVISIONING_PERMISSIONS, GitHubRest

        from .signing import github_app_tokens

        tokens = github_app_tokens(api=settings.github_api, permissions=PROVISIONING_PERMISSIONS)
        return RepoProvisioner(GitHubRest(tokens, api=settings.github_api), openchoreo)
    return RepoProvisioner(github, openchoreo)


def default_knowledge(services: Services):
    from .knowledge import Knowledge

    cfg = services.settings.knowledge
    return Knowledge(cfg, services) if cfg is not None else None


def default_quality(services: Services):
    """Live delivery reads each source from GitHub and OpenChoreo; otherwise one offline source
    stands in for all of them (tests inject results through it)."""
    import os

    from .quality import Quality, load_sources
    from .quality.sources import StubQuality

    cfg = services.settings.quality
    if cfg is None:
        return None
    if os.environ.get("VALXEL_DELIVERY", "stub").lower() == "live":
        sources = load_sources(cfg, services)
    else:
        stub = StubQuality(services)
        sources = {name: stub for name in cfg.sources}
        sources["stub"] = stub
    return Quality(cfg, sources)


def mcp_server(settings: Settings, capability: str, provider: str) -> McpHttp | None:
    """A live MCP connection when the platform has the server's URL and credentials, else None
    (the provider then runs as an offline stub, and /api/products says so)."""
    cfg = settings.tools.server_for(capability, provider)
    if cfg is None or not cfg.url or not cfg.auth_value():
        return None
    return McpHttp(cfg.name, cfg.url, headers={cfg.auth_header: cfg.auth_value()})


def default_diagrams(settings: Settings, github) -> dict[str, DiagramPublisher]:
    lucid = mcp_server(settings, "diagrams", "lucid")
    return {"drawio": DrawioPublisher(github),
            "lucid": LucidPublisher(lucid) if lucid else StubLucidPublisher()}


def default_trackers(settings: Settings) -> dict[str, WorkTracker]:
    out: dict[str, WorkTracker] = {}
    jira = mcp_server(settings, "work_tracking", "jira")
    atl = settings.tools.server_for("work_tracking", "jira")
    out["jira"] = JiraTracker(jira, cloud_id=atl.setting("cloud_id") if atl else "") if jira else StubTracker("jira")
    op = mcp_server(settings, "work_tracking", "openproject")
    op_cfg = settings.tools.server_for("work_tracking", "openproject")
    base = op_cfg.url.rsplit("/mcp", 1)[0] if op_cfg and op_cfg.url else ""
    out["openproject"] = OpenProjectTracker(op, base_url=base) if op else StubTracker("openproject")
    return out


def integration_modes(services: Services) -> dict[str, str]:
    """provider -> "live" or "stub", for the admin view."""
    modes = {}
    for name, pub in services.diagrams.items():
        modes[name] = "stub" if isinstance(pub, StubLucidPublisher) else "live"
    for name, tr in services.trackers.items():
        modes[name] = "stub" if isinstance(tr, StubTracker) else "live"
    from .outcomes.read import sources
    from .outcomes.sources import StubSource

    for name, src in sources(services).items():
        if name != "manual":
            modes[name] = "stub" if isinstance(src, StubSource) else "live"
    return modes


def flag_modes(services: Services, product_ids) -> dict[str, str]:
    """product id -> "flipt", "stub" or "none", for the admin view."""
    describe = getattr(services.flags, "describe", None)
    return {pid: describe(pid) for pid in product_ids} if callable(describe) else {}


class DeliveryConfigError(RuntimeError):
    pass


def default_delivery(settings: Settings) -> tuple[GitHub, OpenChoreo]:
    """VALXEL_DELIVERY=live opens real pull requests and runs real builds and deployments;
    anything else keeps both in memory. Live mode needs the GitHub App and OpenChoreo settings."""
    import os

    if os.environ.get("VALXEL_DELIVERY", "stub").lower() != "live":
        return StubGitHub(), StubOpenChoreo()
    from .integrations.github import DELIVERY_PERMISSIONS, GitHubRest
    from .integrations.openchoreo import OpenChoreoMcp

    from .signing import github_app_configured, github_app_tokens

    missing = [] if github_app_configured() else [
        "VALXEL_GITHUB_APP_ID and a key (VALXEL_GITHUB_APP_KMS_KEY_ID, VALXEL_GITHUB_APP_PRIVATE_KEY or "
        "VALXEL_GITHUB_APP_PRIVATE_KEY_FILE)"]
    oc = settings.tools.servers.get("openchoreo")
    secret = oc.setting("client_secret") if oc else ""
    if not oc or not oc.url:
        missing.append("VALXEL_OPENCHOREO_MCP_URL")
    if not oc or not oc.setting("token_url"):
        missing.append("VALXEL_OPENCHOREO_TOKEN_URL")
    if not secret:
        missing.append("VALXEL_OPENCHOREO_CLIENT_SECRET")
    if missing:
        raise DeliveryConfigError(f"VALXEL_DELIVERY=live needs {', '.join(missing)}")
    tokens = github_app_tokens(api=settings.github_api, permissions=DELIVERY_PERMISSIONS)
    creds = ClientCredentials(oc.setting("token_url"), oc.setting("client_id") or "service_mcp_client", secret,
                              scope=oc.setting("scope"))
    server = McpHttp("openchoreo", oc.url, auth=creds)
    return (GitHubRest(tokens, api=settings.github_api),
            OpenChoreoMcp(server, default_namespace=oc.setting("namespace") or "default",
                          console_url=oc.setting("console_url")))
