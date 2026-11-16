"""Generic runtime: one gateway call per task, any provider. In stub mode the gateway's
StubProvider returns the task's deterministic stub text."""

from __future__ import annotations

from ..gateway import ModelGateway
from .base import AgentResult, AgentTask


class GatewayRuntime:
    def __init__(self, gateway: ModelGateway, name: str = "generic", capabilities: frozenset[str] | None = None):
        self.gateway = gateway
        self.name = name
        self.capabilities = capabilities or frozenset({"text"})

    def run(self, task: AgentTask) -> AgentResult:
        res = self.gateway.complete(task.tags, task.layers, stub_text=task.stub_text, max_tokens=task.max_tokens,
                                     web_searches=task.web_searches)
        out = AgentResult(
            text=res.text,
            tokens=res.total_tokens,
            cost_usd=res.cost_usd,
            request_ids=[res.response.request_id],
            context_fill=res.record.context_fill,
            model=res.record.model,
            runtime=self.name,
        )
        if self.name != "generic":  # standing in for an agent session (stub mode): act like one
            self._session(task, out)
        return out

    def _session(self, task: AgentTask, out: AgentResult) -> None:
        """Stub mode's sessions behave like Claude Code's: an id, resumed when asked (if its
        transcript was kept), and a transcript kept when asked, so the rules around them run."""
        import uuid

        from .. import sessions

        store = sessions.TranscriptStore(self.gateway.settings.artifact_dir)
        fid = task.tags.feature_id
        if task.resume and store.has(fid, task.resume):
            out.session_id, out.resumed = task.resume, True
        else:
            out.session_id = f"stub-{uuid.uuid4().hex[:16]}"
        if task.keep_transcript:
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as d:
                path = Path(d, f"{out.session_id}.jsonl")
                path.write_text(f'{{"type":"stub","session_id":"{out.session_id}","task":"{task.phase}/{task.role}"}}\n')
                store.keep(fid, out.session_id, path)
