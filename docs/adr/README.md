# Architecture decision records

Why the tool is built the way it is: one decision per file, written when it's made, never edited
afterwards except to say it was replaced (`Status: Superseded by NNNN`). A later decision gets a
new number. Records 0001 to 0004 were written after the fact, from the code and its history; their
dates are when the decision took effect.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-langgraph-for-durable-work.md) | Durable work as LangGraph graphs checkpointed in Postgres | Accepted |
| [0003](0003-model-calls-through-a-gateway.md) | Model calls through a gateway: in the process, and the platform's in front of it | Accepted |
| [0004](0004-stub-first.md) | Every integration has an offline stand-in; strict mode refuses them in production | Accepted |
| [0005](0005-one-deployable-with-enforced-seams.md) | One deployable with enforced seams, not separate services yet | Accepted |
| [0006](0006-business-source-license.md) | The Business Source License 1.1 | Accepted |
| [0007](0007-deployment-tiers.md) | Three ways to run the tool (laptop, single VM, cluster) from one image and one set of settings | Accepted |
| [0008](0008-threat-model-agent-plane.md) | Threat model for the agent plane | Accepted |

Write a new one from [the template](template.md): the context (what forces the choice), the
decision, what else was considered, and what follows from it, good and bad. Link it from here.
