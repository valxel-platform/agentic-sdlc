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
from .runtime import AgentRuntime, ClaudeCodeRuntime, GatewayRuntime


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
    quality: object = None  # vrixel_sdlc.quality.Quality, set by build_services
    provisioner: object = None  # vrixel_sdlc.provisioning.RepoProvisioner, set by build_services
    knowledge: object = None  # vrixel_sdlc.knowledge.Knowledge, set by build_services
    features: object = None  # product_id -> feature summaries, set by the engine
    initiatives: object = None  # product_id -> initiatives the engine is running, set by the engine
    feature_titles: object = None  # feature ids -> titles, set by the engine
    release_waits: object = None  # resolves release waits (feature trains, release readiness), set by the engine
    release_for: object = None  # feature_id -> the release it belongs to, set by the engine
    roadmap_fit: object = None  # feature state -> its initiative and budget, set by the engine
    flags: object = None  # vrixel_sdlc.integrations.flags.FlagService
    signals: object = None  # vrixel_sdlc.ops.signals.SignalStore
    incident_signals: object = None  # incident id -> its signals, set by the engine
    suspects: object = None  # (product, repo) -> released features that changed it, set by the engine
    flag_of: object = None  # feature id -> its flag key, set by the engine
    start_hotfix: object = None  # starts an expedited fix feature, set by the engine
    start_intent_change: object = None  # starts a feature or initiative from an incident, set by the engine
    close_incident: object = None  # set by the engine
    incidents: object = None  # product_id -> recent incidents, set by the engine
    costs: object = None  # vrixel_sdlc.ops.cost.CostSource: what released features really cost
    cost_reviews: object = None  # vrixel_sdlc.ops.cost reviews store
    cost_ledger: object = None  # vrixel_sdlc.finops.ledger.CostLedger
    alarms: object = None  # vrixel_sdlc.ops.alarms board: every CloudWatch alarm's current state
    pager: object = None  # vrixel_sdlc.ops.paging.Pager: pages on-call when an incident opens
    changes: object = None  # vrixel_sdlc.ops.changes.ChangeLog: every production change, hash-chained
    inventory_snapshots: object = None  # vrixel_sdlc.ops.inventory snapshots: counts per day, for the trend
    aws_tags: object = None  # vrixel_sdlc.ops.inventory.AwsTags, when VRIXEL_AWS_INVENTORY_ROLE is set
    agents: object = None  # vrixel_sdlc.integrations.openchoreo_agents: OpenChoreo's SRE and FinOps agents
    change_receipts: object = None  # vrixel_sdlc.ops.servicenow receipts store: ServiceNow numbers per change
    effort: object = None  # vrixel_sdlc.pilot.EffortStore: people's effort the pilot report counts
    remediation_record: object = None  # (product, window_days) -> the tool's own fixes' track record, set by the engine
    feature_info: object = None  # feature id -> {risk_tier, components}, set by the engine
    cohort_logs: object = None  # vrixel_sdlc.ops.cohorts log source: users with a flag on against off

    def runtime_for(self, role: str) -> AgentRuntime:
        kind = self.settings.role(role).runtime
        return self.runtimes.get(kind) or self.runtimes["generic"]

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
    providers: dict = {"stub": StubProvider()}
    if settings.mode == "live":
        from .gateway import AnthropicProvider

        providers["anthropic"] = AnthropicProvider()
    gateway = ModelGateway(settings, ledger, providers)
    generic = GatewayRuntime(gateway)
    runtimes: dict[str, AgentRuntime] = {"generic": generic}
    if settings.mode == "live":
        runtimes["claude_code"] = ClaudeCodeRuntime(gateway)
    else:
        runtimes["claude_code"] = GatewayRuntime(
            gateway, name="claude_code", capabilities=frozenset({"text", "native_plugins", "subagents"})
        )
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
        packages=packages or StubPackageRegistry(),
        diagrams=diagrams if diagrams is not None else default_diagrams(settings, github),
        trackers=trackers if trackers is not None else default_trackers(settings),
    )
    svc.quality = default_quality(svc)
    svc.knowledge = default_knowledge(svc)
    svc.flags = default_flags(settings, svc)
    svc.provisioner = default_provisioner(settings, github, openchoreo)
    return svc


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
    return ProductFlags(product_flags, live=os.environ.get("VRIXEL_DELIVERY", "stub").lower() == "live",
                        token_prefix=spec.get("token_env_prefix", "VRIXEL_FLIPT_TOKEN_"))


def default_provisioner(settings: Settings, github, openchoreo):
    """Live delivery creates repos with its own, broader GitHub App token (administration:write),
    used only by the provision step; stubs share the in-memory GitHub."""
    import os
    from pathlib import Path

    from .provisioning import RepoProvisioner

    if os.environ.get("VRIXEL_DELIVERY", "stub").lower() == "live":
        from .governance.sources import GitHubAppTokenProvider
        from .integrations.github import PROVISIONING_PERMISSIONS, GitHubRest

        tokens = GitHubAppTokenProvider(os.environ["VRIXEL_GITHUB_APP_ID"],
                                        Path(os.environ["VRIXEL_GITHUB_APP_PRIVATE_KEY_FILE"]).read_text(),
                                        api=settings.github_api, permissions=PROVISIONING_PERMISSIONS)
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
    if os.environ.get("VRIXEL_DELIVERY", "stub").lower() == "live":
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
    return modes


def flag_modes(services: Services, product_ids) -> dict[str, str]:
    """product id -> "flipt", "stub" or "none", for the admin view."""
    describe = getattr(services.flags, "describe", None)
    return {pid: describe(pid) for pid in product_ids} if callable(describe) else {}


class DeliveryConfigError(RuntimeError):
    pass


def default_delivery(settings: Settings) -> tuple[GitHub, OpenChoreo]:
    """VRIXEL_DELIVERY=live opens real pull requests and runs real builds and deployments;
    anything else keeps both in memory. Live mode needs the GitHub App and OpenChoreo settings."""
    import os
    from pathlib import Path

    if os.environ.get("VRIXEL_DELIVERY", "stub").lower() != "live":
        return StubGitHub(), StubOpenChoreo()
    from .governance.sources import GitHubAppTokenProvider
    from .integrations.github import DELIVERY_PERMISSIONS, GitHubRest
    from .integrations.openchoreo import OpenChoreoMcp

    missing = [v for v in ("VRIXEL_GITHUB_APP_ID", "VRIXEL_GITHUB_APP_PRIVATE_KEY_FILE") if not os.environ.get(v)]
    oc = settings.tools.servers.get("openchoreo")
    secret = oc.setting("client_secret") if oc else ""
    if not oc or not oc.url:
        missing.append("VRIXEL_OPENCHOREO_MCP_URL")
    if not oc or not oc.setting("token_url"):
        missing.append("VRIXEL_OPENCHOREO_TOKEN_URL")
    if not secret:
        missing.append("VRIXEL_OPENCHOREO_CLIENT_SECRET")
    if missing:
        raise DeliveryConfigError(f"VRIXEL_DELIVERY=live needs {', '.join(missing)}")
    tokens = GitHubAppTokenProvider(os.environ["VRIXEL_GITHUB_APP_ID"],
                                    Path(os.environ["VRIXEL_GITHUB_APP_PRIVATE_KEY_FILE"]).read_text(),
                                    api=settings.github_api, permissions=DELIVERY_PERMISSIONS)
    creds = ClientCredentials(oc.setting("token_url"), oc.setting("client_id") or "service_mcp_client", secret,
                              scope=oc.setting("scope"))
    server = McpHttp("openchoreo", oc.url, auth=creds)
    return (GitHubRest(tokens, api=settings.github_api),
            OpenChoreoMcp(server, default_namespace=oc.setting("namespace") or "default",
                          console_url=oc.setting("console_url")))
