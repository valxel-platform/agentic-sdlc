# 0009. The agent harness: Claude Code and Codex for code, measured against LangChain

Date: 2026-11-23
Status: Accepted (2026-11-23; the document-phase trial is parked)
Deciders: Sunil Vallabhaneni

## Context

ADR 0002 chose LangGraph for the tool's long-running work: features, gates, releases and incidents.
It never recorded what runs inside a phase: the agent harness, meaning the loop that calls a model,
uses tools and keeps context. Today, the harness is behind one interface (`runtime/base.py`,
`AgentRuntime`) with three implementations, picked per role in `config/models.yaml`:

| Runtime | What it is | Roles today |
|---|---|---|
| `claude_code` | Claude Code, headless through the Claude Agent SDK (`runtime/claude_code.py`) | architect, coder, reviewer |
| `codex` | OpenAI's Codex CLI, for OpenAI and self-hosted models (`runtime/codex.py`) | coder variants (`coder_codex`, `coder_open`) |
| `generic` | One model call through the gateway, no tools (`runtime/generic.py`) | discovery, spec, designer, planner, critic, release, ops, independent reviewer and others |

LangChain is the obvious alternative, because we already depend on its LangGraph. As of November 2026:

- **LangChain 1.x** makes `create_agent` the standard agent: a model, tools and a prompt, with
  middleware around the loop for summarization, PII redaction, human approval, call limits and
  prompt caching. It runs on LangGraph. `langchain` 1.4.0 (September 2026) is current.
- **Deep Agents** is LangChain's harness for long-running work, including coding:
  - a filesystem with `ls`, `read_file`, `write_file`, `edit_file`, `glob` and `grep`;
  - subagents, skills and context management;
  - sandbox backends that add an `execute` tool for shell commands.

  The sandbox providers LangChain ships are Modal, Daytona, E2B, Vercel and LangSmith Sandboxes,
  plus a local shell backend. A provider implements only `execute()`. Deep Agents is still 0.x:
  v0.7 (July 2026) cut the base prompt and dropped the to-do list. Managed Deep Agents is in public
  beta (v0.10, October 2026). The Thoughtworks Radar lists Deep Agents under Trial (November 2026).

## Comparison

| Concern | Ours | LangChain | Assessment |
|---|---|---|---|
| Long-running work, gates, resume | LangGraph graphs, gates as `interrupt`, Postgres checkpoints (ADR 0002) | The same LangGraph underneath `create_agent` and Deep Agents | Already shared: nothing to change |
| Coding agent: edit, build, test, iterate | Claude Code's and Codex's own tools and loop, maintained by their vendors and used daily on real repos | Deep Agents' file tools plus `execute`, or Deep Agents Code | Ours is the more proven coding harness. Deep Agents would need to match it on our coder evals before we'd switch |
| Where code runs | A gVisor pod per session (Agent Sandbox), the shell sandboxed again inside it, egress limited to the allowlist, nothing kept afterwards (T-121) | A sandbox backend. The ready-made ones are hosted services (Modal, Daytona, E2B, Vercel, LangSmith) | Hosted sandboxes would send customer code out of the account, against the hosted and self-hosted promise. We'd have to write a backend for Agent Sandbox, which is possible since it needs only `execute()` |
| Model access | Every call through the gateway: per-session tokens, a hard budget cap, redaction, audit (ADR 0003, T-137) | Chat models can point at any base URL; PII and limit middleware | Fits either way. The gateway stays in charge; LangChain's redaction and limits would duplicate it |
| Choice of model | Claude through Claude Code; OpenAI and self-hosted through Codex; any provider through `generic` | Any provider through LangChain's integrations | LangChain is broader, but the gateway and Codex already cover the models we use |
| Context in long sessions | Claude Code's own compaction | Summarization middleware; Deep Agents' context management | Even |
| Structured output | JSON schema enforced by Claude Code, and every reply validated (`agents/structured.py`) | Structured response formats | Even |
| Human approval | Gates between phases, recorded with separation of duties (T-133) | Human-in-the-loop middleware, per tool call | Ours is at the level the business needs. Per-call approval isn't a requirement |
| Skills and plugins | Claude Code plugins, with skills loaded on demand and recorded; hooks and connectors stripped | Deep Agents skills | Even. Ours is tied to the Claude Code plugin format |
| Tracing and evals | OpenTelemetry to Langfuse; cases kept, labelled at gates and replayed (T-145) | LangSmith natively, OpenTelemetry too | Ours is independent of a vendor's hosted service |
| Upkeep | Claude Code and Codex are CLIs with their own releases, pinned; our code is ~1,000 lines of runtimes plus the session runner | Libraries we'd pin; Deep Agents is 0.x and still changing shape between minors | Similar effort; Deep Agents carries more churn risk today |
| Document phases (spec, design, plan, review) | One call, no tools: the context is pasted into the prompt | `create_agent` with tools (read the repo, search the knowledge base) and middleware | **The one real gap.** A tool-using agent might write better documents than one call, at lower cost than a Claude Code session |

## Decision

1. **Keep Claude Code and Codex as the harness for code work** (architect, coder, reviewer). Don't
   adopt Deep Agents for coding now. It would mean a sandbox backend of our own and a less proven
   coding loop, with no gain on what customers judge: code that passes review and CI.
2. **Keep LangGraph for long-running work** (ADR 0002). That is the part of LangChain we already use.
3. **Measure LangChain where it could win: the document phases.** Add a `langchain` runtime behind
   `AgentRuntime`: `create_agent`, its model pointed at the gateway, read-only tools (repo files at
   the feature's commit, the knowledge base) and no middleware the gateway already covers. Run it as
   a shadow variant for spec, designer and planner. Replay it on past cases with the existing evals,
   and adopt it per role only if it matches or beats the current runtime on quality, at no more
   cost.
   Parked (2026-11-23): accepted, not yet scheduled; tracked as T-147.
4. Any runtime added stays behind `AgentRuntime` and the gateway, and runs in the session sandbox
   when it touches code. That makes the choice per role, and reversible.

## Alternatives

- **LangChain for everything (Deep Agents for code, `create_agent` for documents):**
  - Pro: one framework, any provider.
  - Con: a less proven coding loop, plus a sandbox backend to build and keep.
  - Con: Deep Agents' 0.x churn would land in the most critical phase.
- **Claude Code for everything, documents included:**
  - Pro: one harness.
  - Con: a full coding session costs more, and its tools are more than a document phase needs.
  - Con: the architect already runs this way, which shows the cost.
- **Our own tool-calling loop for document phases:**
  - Pro: small and fully ours.
  - Con: `create_agent` already gives the loop, limits and summarization, and runs on the
    LangGraph we already depend on.
- **Other agent SDKs (OpenAI Agents SDK, Pydantic AI):** they cover the same ground as
  `create_agent` with less tie-in to LangGraph. Worth checking if the document-phase trial shows
  value but `create_agent` fits poorly.

## Consequences

- **The comparison is written down.** Changing harness is a per-role setting in
  `config/models.yaml`, checked by evals, not a rewrite.
- **A new dependency for the trial:** `langchain` and a provider package, pinned below the next
  major, in the live extras only.
- **The coding harness stays tied to two vendors' CLIs.** Their releases are pinned and their
  behaviour is checked by the coder evals and drift replays.
- **Revisit Deep Agents for code when either:**
  - there's a sandbox backend for Agent Sandbox (ours or upstream) and Deep Agents matches Claude
    Code on our coder evals; or
  - a customer needs a model that neither Claude Code nor Codex can run.

## Sources

- [LangChain v1 release notes](https://docs.langchain.com/oss/python/releases/langchain-v1)
- [LangChain and LangGraph 1.0](https://www.langchain.com/blog/langchain-langgraph-1dot0)
- [Built-in middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)
- [LangChain changelog](https://docs.langchain.com/oss/python/releases/changelog)
- [Deep Agents sandboxes](https://docs.langchain.com/oss/python/deepagents/sandboxes)
- [Deep Agents backends](https://docs.langchain.com/oss/python/deepagents/backends)
- [Thoughtworks Radar: Deep Agents](https://www.thoughtworks.com/radar/languages-and-frameworks/deep-agents)
