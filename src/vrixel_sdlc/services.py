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
from .integrations.mcp_client import McpHttp
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

    def runtime_for(self, role: str) -> AgentRuntime:
        kind = self.settings.role(role).runtime
        return self.runtimes.get(kind) or self.runtimes["generic"]

    def snapshot(self, product_id: str, pins: dict | None = None) -> GovernanceSnapshot:
        """The product's governance, read from its own org. Pass the feature's pins inside a
        feature thread; omit them to resolve the current commit (intake and admin views)."""
        return self.governance.snapshot(product_id, pins)


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
    github = github or StubGitHub()
    return Services(
        settings=settings,
        ledger=ledger,
        gateway=gateway,
        artifacts=ArtifactStore(settings.artifact_dir),
        github=github,
        openchoreo=openchoreo or StubOpenChoreo(),
        catalog=catalog or default_catalog(),
        runtimes=runtimes,
        governance=GovernanceLoader(settings, governance_source or build_source(settings)),
        packages=packages or StubPackageRegistry(),
        diagrams=diagrams if diagrams is not None else default_diagrams(settings, github),
        trackers=trackers if trackers is not None else default_trackers(settings),
    )


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
