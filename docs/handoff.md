# Handoff: where Valxel SDLC stands (Nov 13, 2026)

Notes for a new chat picking up this work. They summarize a long working session; the code and
commit history in this repo are the detailed record.

## The product

Valxel SDLC (formerly Vrixel) is an agentic software delivery tool: a LangGraph pipeline in which
agents draft each phase and people approve at gates G0–G6. New products go through an inception
thread (research → brief → brief gate → product architecture and roadmap → product gate →
setup). It deploys products with OpenChoreo on AWS.

## Repositories (GitHub org `valxel-platform`)

The org was `vrixel-tools`, briefly `valxel-tools`, and is now `valxel-platform`.

- **agentic-sdlc:** the tool itself (Python package `valxel_sdlc`, web app in `web/`).
- **Templates:** template-service, template-bff, template-event-worker, template-agent,
  template-mcp-server, template-web, template-mobile, template-acceptance.
- **Config and governance:** sdlc-config (what the tool reads), sdlc-governance (constitution,
  policies, OpenChoreo platform files).
- **Platform:** platform-foundation (OpenTofu, GitOps), platform-resources, platform-security.

Every repo was renamed from Vrixel to Valxel and merged on Nov 13. Branch `remove-vrixel` in
agentic-sdlc deletes the last of the old names (the fallback that still read `VRIXEL_*` settings,
renamed `vrixel_*` tables and posted `vrixel/` checks); nothing runs under the old name, so it is
safe to merge. Open its PR if it isn't open yet.

## Recent work, all merged

- **#78 Capacity and environments:** the architect's NFRs drive a capacity plan (`capacity.py`,
  `environments.py`). Products start small and step up by stage (pilot, growth, scale).
  OpenChoreo environments are created per product at the product gate.
- **#79, #80 Supply chain:** sandboxed Claude Code sessions for coding agents (`sandbox.py`,
  `config/egress.yaml`), scoped GitHub App tokens, cosign signing with SBOM and SLSA provenance,
  Kyverno image verification, a new-dependencies check (`dependencies.py`: registry existence,
  30-day age, license, Scorecard), images pinned by digest including `COPY --from`, Gradle
  dependency locking and checksums, Inspector findings to incidents.
- **#81 and template-agent #24 Agent component type:** agents deploy as their own OpenChoreo type.
  The pod gets no Kubernetes token, runs non-root on a read-only file system, can reach only the
  LLM gateway, MCP servers and database, and runs under gVisor for PHI and payments products.
- **#83 Rename to Valxel.**

## Decisions the user made (keep these unless they say otherwise)

- **Start small:** an entrepreneur shouldn't pay for full production before there are customers.
  The capacity model sizes for today's load but shows how it scales to future load.
- **Headroom 60%** over the busy-hour load; plan for 10x growth across the stages.
- **Availability tiers 1–3.** A PHI (health data) pilot at tier 3 is acceptable.
- **Clusters:** each product gets its own clusters. All products share one OpenChoreo control
  plane, and each product has two data planes (non-production and production).
- **Observability:** one shared plane for non-production, one per product for production.
  Products are built one at a time, not rolled out together.
- **Staging** is a fixed small size. **Production** capacity is applied by the tool.
- **Scaling on CPU only**, event workers included.
- **Databases:** a Multi-AZ medium database for production. Dev and staging databases pause when
  idle.
- **Agents (Nov 12):** use the hardened agent component type now. Running model-written code in a
  separate sandbox comes later (see the to-do list).
- **PHI** goes only to providers with a BAA. Real product names stay out of public repos.

## What's next

### 1. Architecture diagrams at the gates (asked for on Nov 13)

Today:

- **Features:** the HLD requires a Mermaid component flowchart (`## Diagram`) and sequence
  diagrams per journey with a failure path (`## Flows`). `publish_diagram` in `graph/phase.py`
  publishes the flowchart to the product's diagram tool (Lucid, or draw.io committed on the
  feature branch, see `integrations/diagrams.py`), versions it and links it. G4 reviews it.
- **Inception:** the product architecture (`inception/formats.py`: Context, Components, Load,
  Scaling, Failure, NFRs, ADRs…) has no diagram. The product gate reviews text only.
- **Initiatives:** no architecture view at all; only their features get diagrams.
- **Gate page:** the web app (react-markdown) shows Mermaid as code, not a picture.

To do:

1. Inception: require Mermaid diagrams in the product architecture — a system context and
   container view, a deployment view of the environments and their OpenChoreo data planes, and
   the main journeys with failure paths. Publish them like the feature diagram and show them at
   the product gate.
2. Gate page: render Mermaid diagrams on the page, so G4 and the product gate show pictures.
3. Optional: one overview diagram per initiative showing how its features connect.

### 2. The to-do list

The user keeps a "Valxel SDLC to-do" doc in claude.ai; ask them for its link. Highlights:

- Code-execution sandbox for agents: a "run code" MCP tool that claims a pre-warmed
  [Agent Sandbox](https://agent-sandbox.sigs.k8s.io/docs/getting_started/overview/) (gVisor, no
  network, a time limit). G4 requires it when an agent runs generated code.
- Access for live runs (AWS cluster permissions, OpenChoreo platform-admin); most items wait on it.
- Dry-run the OpenChoreo platform files and fill the `REPLACE_WITH_DIGEST` markers.
- One real feature through staging; a live coder sandbox session; the step G failure test.
- Staging load tests report throughput per pod; read production's busy hour on a schedule.
- Price fixed costs (NAT, load balancers, Kafka, the production observability plane).
- Wire Amazon Inspector; watch the first Dependabot PR; pin the scanner images CI runs.
- Verify the agent type live (gVisor RuntimeClass, `valxel.io/role` labels, `egressCIDRs`).

That doc still says `VRIXEL_AGENT_SANDBOX_WEAKER` and `vrixel.io/role`; they are now
`VALXEL_AGENT_SANDBOX_WEAKER` and `valxel.io/role`. Its links to #80, #81, template-agent #24 and
template-mcp-server #18 point at the old org, and those PRs are merged.

## Working conventions

- Commits as `vallab76 <vallab76@users.noreply.github.com>`; commit messages and PR bodies carry
  the Claude Code attribution lines.
- Every change goes through a PR, with CI watched to green; the user merges.
- Tests: `uv run pytest`. Postgres tests need `VALXEL_TEST_DATABASE_URL`. Web: `npm run build` in
  `web/`.
