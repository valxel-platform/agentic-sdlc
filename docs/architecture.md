# Architecture

How the tool fits together, at a high level. The guides for [operators](operators.md), [product teams](product-teams.md) and [contributors](contributors.md) have the detail for each
part; [How the design maps to code](contributors.md#how-the-design-maps-to-code) and
[Layout](contributors.md#layout) list the modules.

The tool is one Python service with one Postgres database. Agents do the work, people decide at
gates, and each product's own governance repo sets the rules the tool must follow. Everything
outside the dashed frame below belongs to the products or to third parties, and the tool reaches each
through an interface with an offline stub, so the whole pipeline runs deterministically in tests.

![Valxel SDLC: people, production signals and GitHub webhooks enter its API; the engine runs the roadmap, feature pipeline and incident workflows, which use agents through the model gateway and act under the production policy through GitHub, OpenChoreo, Flipt, PagerDuty, ServiceNow, Jira or OpenProject, Lucid or draw.io, AWS and the Backstage catalog. It reads governance pinned per feature and deploys products, which send signals back.](architecture.svg)

The diagram is [architecture.svg](architecture.svg). [architecture.html](architecture.html) is the
same page with the flows and rules laid out together: download it or serve the `docs/` folder
with GitHub Pages to view it in a browser.

The GitHub Pages site also has a branded [high-level design](hld.html) and the
[full architecture diagram](architecture-diagram.html), starting from [index.html](index.html).

Where the parts run, on a laptop and in a customer's cluster, and what each may reach:
[deployment diagrams](architecture/README.md).

## How it's stacked

The tool has three layers, and each builds only on the ones below it. The **root** holds the
front doors (API, web console, A2A and MCP) and wires everything together. Four **seams** do the
product work (intake, delivery, the roadmap and operations), each independent of the others. The
**core** supplies what every layer shares: running agents, the model gateway, checks, workflow
rules, governance, integrations, and ports to AWS, Azure and Google Cloud.

![Valxel's capabilities in three stacked layers. Root: front doors, feature engine, service wiring, self-evaluation, lite hosting. Seams: intake (guided intake, product inception), delivery (pipeline and gates, design previews, build, merge and release, repos and records), roadmap (initiatives and releases, outcome loop) and ops (incidents and signals, costs and inventory, security upkeep). Core: agent runtime, model gateway, checks and quality policy, workflow rules, governance and records, components and resources, production rules, integrations, cloud ports for AWS, Azure and Google Cloud, trust and access.](architecture/capability-layers.png)

The same layers with what each capability does:
[capability-layers-detailed.png](architecture/capability-layers-detailed.png).

The rule that keeps them apart is enforced, not only drawn: `config/seams.yaml` places every module
in a layer and `tests/test_seams.py` fails on any import that points upward or across seams
([ADR 0005](adr/0005-one-deployable-with-enforced-seams.md)).

## How work moves through it

**Idea to production**: one feature thread, across every repo it touches.

1. The intake agent drafts the intent with the person and checks what the product already has.
2. Discovery, then spec, approved at G1 and G2.
3. Design and architecture run in parallel, approved at G3 and G4, then join. The architect's
   diagram is published to Lucid or draw.io for G4 reviewers.
4. New repos are provisioned from templates. The planner splits the work: one coder agent per
   task, one reviewer per pull request.
5. CI must go green. High-risk and regulated changes need G5. The merge train merges libraries,
   then providers, then consumers.
6. OpenChoreo builds the merge commits and deploys to dev, then staging.
7. Release managers approve production and a rollout plan at G6.
8. The flag steps through 5, 25, 50 and 100%. It holds on incidents in or around the changed
   components and stops if users with it on fail more than users with it off.

**Signal to resolution**: the tool does the heavy lifting; on-call is paged only when it can't.

1. A signal joins an open incident or opens one. A SEV1 sends on-call a low-urgency heads-up.
2. OpenChoreo's root-cause agent investigates, on a provider allowed for the data class, while the
   tool correlates recent changes.
3. Triage proposes actions. Suspects are recent releases and features still rolling out nearby.
4. When the policy allows and the analysis agrees, the tool turns the flag off or rolls back, and
   records the change.
5. It watches for recovery. If it can't act, its action fails, or the failure continues, it pages
   on-call urgently.
6. The fix goes back through the pipeline as a hotfix, and the postmortem is drafted into the
   product's governance repo.

**What every step leaves behind**, for audit and for looking back:

- Each decision: who approved, at which gate, with how many active minutes.
- Each production change: who or what made it and why, with a ServiceNow receipt.
- Each model call: role, model, tokens, cache hits and cost, in the usage ledger.
- Each feature's cost in production, from FinOps, beside its build cost.
- The resource inventory: what runs in production against the lower environments.
- Traces of every thread, and postmortems and ADRs in Git.

## Design rules

- **Agents work, people decide.** Gates G0 to G6 wait for an approver named in the product's own
  governance repo. Three failed evaluator loops, or a spent budget, hand the phase to a person.
- **Products own their rules.** The tool keeps no product settings. It reads each product's
  governance repo and pins it by commit for the life of a feature. Products can tighten the
  platform's rules, never loosen them.
- **Products own their documents.** When a gate is approved, the documents it approved (discovery,
  PRD, UX and its clickable prototype, HLD, plan, release record, intake attachments) go to the product's
  governance repo under `features/<feature>/`, with who approved them. Agent transcripts never do;
  attachments of PHI or payments products are listed by digest, not committed.
- **Data class picks the model.** The gateway routes PHI and payments work only to providers
  covered for it. The same rule decides which provider OpenChoreo's root-cause agent may use.
- **Production stays human, with two exceptions.** Promotion to production is a person's decision
  at G6. The tool may act alone only to turn off a recent feature's flag or roll back a recent
  release, when the policy allows it and the analysis agrees.
- **Threads survive restarts.** LangGraph checkpoints every step in Postgres, so waits on people,
  CI and deploys resume after a restart.
- **Every integration is an interface.** Stub mode runs offline and deterministically; live mode
  swaps in Claude Code, the Claude API and the real systems one interface at a time.

## Keeping this current

Update this page, `architecture.svg` and `architecture.html` in the same pull request as any change
that adds, removes or moves a part of the tool shown here. A new or moved capability also updates
the [capability layers](architecture/README.md#capability-layers): edit the two HTML sources and
re-render the PNGs. The [deployment diagrams](architecture/README.md) follow the
same rule for changes to the Compose bundles, the Helm chart or the network policy.
