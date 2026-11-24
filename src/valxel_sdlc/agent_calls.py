"""Calling an agent: one session for a role, with its prompt layers, caps and fallbacks.

`call_agent` is a delivery phase's call (lessons learned, budgets, resuming sessions, fallbacks when
a provider is unavailable); `run_agent` is the plain call the roadmap's graphs and incidents make.
The core's (ADR 0005): the pipeline, the roadmap, ops and the lessons all call agents, and this
imports no seam. Both act only through the services they're handed.
"""

from __future__ import annotations

import dataclasses
import logging

from . import lessons
from .agents import PHASE_SPECS
from .agents.context import build_layers, skills_dir_of
from .gateway import CallTags, ProviderUnavailable
from .runtime import AgentResult, AgentTask

log = logging.getLogger(__name__)


NO_LESSON_ROLES = ("knowledge", "router", "intake")


def call_agent(services, *, role: str, phase: str, ctx: dict, skills: tuple[str, ...], tail: str,
               stub_text: str | None, max_tokens: int = 4096, schema: dict | None = None,
               budget_usd: float | None = None, cwd: str | None = None,
               box: dict | None = None, task_id: str = "", prev_session: dict | None = None,
               resume_tail: str = "", keep_transcript: bool = False, images: tuple = ()) -> AgentResult:
    """One agent session. Its dollar cap is the role's, and never more than what's left of the
    feature's budget (ctx budget_remaining_usd, or `budget_usd` when the caller knows better).

    The same task (prompt layers, reply schema, caps) goes to the role's fallbacks, in order, when
    the role's provider is unavailable; the result says which role did the work."""
    snap = services.snapshot(ctx["product_id"], ctx.get("governance"))
    product = snap.product
    learned = lessons.instruction(snap.lessons, phase) if role not in NO_LESSON_ROLES else ""
    if learned:  # what people taught earlier features of this product, for this phase
        tail = f"{tail}\n\n{learned}"
    variant = ctx.get("config_variant", "")  # a canary feature's agents run with the candidate config
    layers = build_layers(
        role=role, skills=skills, constitution=snap.constitution, product=product,
        parent_texts=ctx.get("parent_texts", {}), tail=tail, product_context=snap.product_context,
        design_knowledge=ctx.get("knowledge_text", ""), skills_dir=skills_dir_of(services, variant),
        caps=services.settings.context_caps,
    )
    for layer, note in (layers.meta.get("trimmed") or {}).items():
        log.info("%s %s/%s: %s cut to its cap: %s", ctx["feature_id"], phase, role, layer, note)
    left = budget_usd if budget_usd is not None else ctx.get("budget_remaining_usd")
    candidates = services.roles_for(role, product.data_class, variant) if variant else services.roles_for(role, product.data_class)
    for i, who in enumerate(candidates):
        runtime = services.runtime_for(who, variant) if variant else services.runtime_for(who)
        missing = PHASE_SPECS[phase].required_capabilities - runtime.capabilities
        if missing:
            if i == 0:
                raise RuntimeError(f"Runtime {runtime.name} lacks {sorted(missing)} required by phase {phase}")
            continue  # a fallback that can't do this phase's work is skipped
        tags = CallTags(
            product_id=ctx["product_id"], feature_id=ctx["feature_id"], phase=phase, role=who,
            data_class=product.data_class, workspace=product.workspace, prompt_version=f"{phase}@1",
        )
        # Resume the task's last session, or start fresh: from its handoff when it left one
        # (valxel_sdlc.sessions). A fallback to another model never resumes.
        session_args, layers_for = {}, layers
        if prev_session is not None or keep_transcript:
            from . import sessions

            handoff = handoff_text(services, prev_session)
            fresh = layers if not handoff else dataclasses.replace(layers, tail=layers.tail + sessions.picking_up(handoff))
            decision = sessions.may_resume(
                prev_session, task_id=task_id, role=who, model=services.settings.role(who).model, runtime=runtime.name,
                transcripts=sessions.TranscriptStore(services.settings.artifact_dir), feature_id=ctx["feature_id"])
            if prev_session:
                log.info("%s %s %s: %s session %s: %s", ctx["feature_id"], task_id, who,
                         "resuming" if decision.resume else "not resuming", prev_session.get("session_id"), decision.why)
            if decision.resume and resume_tail:
                layers_for = dataclasses.replace(layers, tail=resume_tail)
                session_args = {"resume": prev_session["session_id"], "fresh_tail": fresh.tail, "session_mode": "resumed"}
            else:
                layers_for = fresh
                session_args = {"session_mode": "handoff" if handoff else "fresh"}
            session_args["keep_transcript"] = keep_transcript
        try:
            res = runtime.run(AgentTask(role=who, phase=phase, tags=tags, layers=layers_for, skills=tuple(skills),
                                        stub_text=stub_text, max_tokens=max_tokens, schema=schema, images=tuple(images),
                                        max_budget_usd=None if left is None else max(0.0, float(left)),
                                        # A box is a session's own writable checkout; a bare cwd is a
                                        # read-only one (valxel_sdlc.sandbox, valxel_sdlc.workspace).
                                        cwd=(box or {}).get("cwd") or cwd,
                                        read_only=(bool(cwd) and not box) or bool((box or {}).get("read_only")),
                                        sandbox=box, **session_args))
        except ProviderUnavailable as exc:
            if i == len(candidates) - 1:
                raise
            log.warning("%s %s/%s: %s; handing the task to %s", ctx["feature_id"], phase, who, exc, candidates[i + 1])
            continue
        res.role = who
        return res
    raise RuntimeError(f"No runtime for role {role} can do phase {phase}")


def handoff_text(services, session: dict | None) -> str:
    """The handoff an earlier session on the task left, or ""."""
    ref = (session or {}).get("handoff")
    if not ref:
        return ""
    try:
        return services.artifacts.load(ref)
    except OSError:
        return ""



def run_agent(services, *, role: str, product_id: str, thread_id: str, phase: str, skills: tuple[str, ...],
              tail: str, stub_text: str, max_tokens: int = 3000):
    snap = services.snapshot(product_id)
    layers = build_layers(role=role, skills=skills, constitution=snap.constitution, product=snap.product,
                          parent_texts={}, tail=tail, product_context=snap.product_context,
                          skills_dir=skills_dir_of(services), caps=services.settings.context_caps)
    tags = CallTags(product_id=product_id, feature_id=thread_id, phase=phase, role=role,
                    data_class=snap.product.data_class, workspace=snap.product.workspace, prompt_version=f"{phase}@1")
    return services.runtime_for(role).run(AgentTask(role=role, phase=phase, tags=tags, layers=layers, skills=tuple(skills),
                                                    stub_text=stub_text, max_tokens=max_tokens))

