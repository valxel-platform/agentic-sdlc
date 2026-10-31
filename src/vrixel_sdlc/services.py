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
    return Services(
        settings=settings,
        ledger=ledger,
        gateway=gateway,
        artifacts=ArtifactStore(settings.artifact_dir),
        github=github or StubGitHub(),
        openchoreo=openchoreo or StubOpenChoreo(),
        catalog=catalog or default_catalog(),
        runtimes=runtimes,
        governance=GovernanceLoader(settings, governance_source or build_source(settings)),
        packages=packages or StubPackageRegistry(),
    )
