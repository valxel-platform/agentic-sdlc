"""Model gateway: resolves a role to a provider and model, enforces the data-class allowlist
(VX-7), calls the provider and writes one ledger record per call."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import Settings
from .ledger import Ledger, UsageRecord
from .pricing import estimate_cost
from .prompt import PromptLayers
from .providers import ModelRequest, ModelResponse, Provider


def _trace_fields() -> dict:
    """The active agent session's trace, and its task and attempt (vrixel_sdlc.telemetry)."""
    from ..telemetry import current_ids, current_labels

    trace_id, span_id = current_ids()
    lab = current_labels()
    return {"trace_id": trace_id, "span_id": span_id, "task_id": lab.get("task", ""), "attempt": lab.get("attempt", "")}


class PolicyViolation(RuntimeError):
    """A request would send a data class to a provider it is not allowed to reach."""


@dataclass
class CallTags:
    product_id: str
    feature_id: str
    phase: str
    role: str
    data_class: str
    workspace: str
    prompt_version: str = "v1"


@dataclass
class GatewayResult:
    text: str
    response: ModelResponse
    cost_usd: float
    record: UsageRecord

    @property
    def total_tokens(self) -> int:
        r = self.response
        return r.input_tokens + r.cache_creation_input_tokens + r.cache_read_input_tokens + r.output_tokens


class ModelGateway:
    def __init__(self, settings: Settings, ledger: Ledger, providers: dict[str, Provider]) -> None:
        self.settings = settings
        self.ledger = ledger
        self.providers = providers
        from ..pilot import config_version

        self.config_version = config_version(settings)

    def allowed(self, provider: str, data_class: str) -> bool:
        allowed = self.settings.data_classes.get(data_class, [])
        return "*" in allowed or provider in allowed

    def _provider_for(self, provider_name: str) -> Provider:
        if self.settings.mode == "stub":
            return self.providers["stub"]
        try:
            return self.providers[provider_name]
        except KeyError as exc:
            raise RuntimeError(f"No provider configured for {provider_name!r}") from exc

    def complete(
        self,
        tags: CallTags,
        layers: PromptLayers,
        *,
        stub_text: str | None = None,
        max_tokens: int = 4096,
    ) -> GatewayResult:
        role = self.settings.role(tags.role)
        if not self.allowed(role.provider, tags.data_class):
            raise PolicyViolation(
                f"{tags.data_class} data may not be sent to provider {role.provider!r} "
                f"(role {tags.role}, product {tags.product_id})"
            )
        req = ModelRequest(
            model=role.model,
            layers=layers,
            workspace=tags.workspace,
            ttl=role.ttl,
            max_tokens=max_tokens,
            stub_text=stub_text,
        )
        started = time.monotonic()
        resp = self._provider_for(role.provider).complete(req)
        return self._record(tags, role.runtime, role.provider, role.model, role.ttl, layers, resp,
                            duration_s=time.monotonic() - started)

    def record_external(
        self, tags: CallTags, *, runtime: str, model: str, layers: PromptLayers, response: ModelResponse,
        duration_s: float = 0.0,
    ) -> GatewayResult:
        """Ledger entry for a call made outside the gateway (e.g. the Claude Agent SDK)."""
        provider = model.split("/", 1)[0]
        return self._record(tags, runtime, provider, model, self.settings.role(tags.role).ttl, layers, response,
                            duration_s=duration_s)

    def _record(self, tags, runtime, provider, model, ttl, layers, resp, duration_s: float = 0.0) -> GatewayResult:
        usd, basis = estimate_cost(
            model,
            input_tokens=resp.input_tokens,
            cache_creation_input_tokens=resp.cache_creation_input_tokens,
            cache_read_input_tokens=resp.cache_read_input_tokens,
            output_tokens=resp.output_tokens,
            ttl=ttl,
            prices=self.settings.prices,
        )
        rec = UsageRecord(
            ts=datetime.now(timezone.utc).isoformat(),
            product_id=tags.product_id,
            feature_id=tags.feature_id,
            phase=tags.phase,
            role=tags.role,
            runtime=runtime,
            provider=provider,
            model=model,
            workspace=tags.workspace,
            input_tokens=resp.input_tokens,
            cache_creation_input_tokens=resp.cache_creation_input_tokens,
            cache_read_input_tokens=resp.cache_read_input_tokens,
            output_tokens=resp.output_tokens,
            cache_ttl=ttl,
            prompt_version=tags.prompt_version,
            prefix_hashes=layers.prefix_hashes(),
            est_cost_usd=usd,
            cost_basis=basis,
            request_id=resp.request_id,
            config_version=self.config_version,
            duration_s=round(float(duration_s), 3),
            **_trace_fields(),
        )
        self.ledger.record(rec)
        return GatewayResult(text=resp.text, response=resp, cost_usd=usd, record=rec)
