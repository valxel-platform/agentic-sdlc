"""Service container wired into graph nodes: gateway, ledger, runtimes, stores, integrations."""

from __future__ import annotations

from dataclasses import dataclass

from .artifacts import ArtifactStore
from .config import Settings, load_settings
from .constitution import Constitution, load_constitution
from .gateway import InMemoryLedger, ModelGateway, StubProvider
from .gateway.ledger import Ledger
from .integrations import (
    CapabilityCatalog,
    GitHub,
    OpenChoreo,
    StubGitHub,
    StubOpenChoreo,
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

    def runtime_for(self, role: str) -> AgentRuntime:
        kind = self.settings.role(role).runtime
        return self.runtimes.get(kind) or self.runtimes["generic"]

    def constitution(self, product_id: str) -> Constitution:
        product = self.settings.product(product_id)
        return load_constitution(str(self.settings.constitution_dir), product.constitution)


def build_services(
    settings: Settings | None = None,
    *,
    ledger: Ledger | None = None,
    github: GitHub | None = None,
    openchoreo: OpenChoreo | None = None,
    catalog: CapabilityCatalog | None = None,
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
    )
