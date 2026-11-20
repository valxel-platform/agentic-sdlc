# Runbook: tool health and drift alerts

Use this runbook when a `Valxel tool health`, `Valxel drift evals` or `Valxel safe mode` message
arrives on the notify webhook (VALXEL_NOTIFY_WEBHOOK). These alerts are about Valxel's own agents,
not about a product's code. They mean the critic, a writer or a reviewer is doing worse than its
baseline on real work, or on the pinned evaluation cases.

Who: the tool's owners (VALXEL_EVAL_APPROVERS) run it. A product's owners join when the cause is
in their product.

The five steps are **triage, contain, fix by cause, verify, close**. Most of this is already done
for you: each alert says what changed, and safe mode contains the risk on its own.

---

## 1. Triage (first 15 minutes)

1. **Read the alert's "What changed" line.** It names which of the three usual causes moved in
   the window (valxel_sdlc.what_changed):
   - **setup**: a config version that went live in the window (models, prompts, skills, rules)
   - **model**: a role answered by a model it never answered before. This is a new snapshot
     behind the same name, or a fallback taking over.
   - **product**: a product's governance changed. That means lessons added or removed, a new
     constitution or context commit, or a prompt section that grew by at least 25% and 500 tokens.
   - **none of these**: the line says so and points at the provider's serving.
2. **Open the full picture:**
   - `GET /api/reports/health?fresh=true`: every tripped rule with its value and baseline,
     `what_changed` in full, and `safe_mode` (each product in it, for how many hours, and why).
   - `GET /api/evals/drift`: the latest weekly run, the last 12 runs, and the anchor cases.
   - `GET /api/safe-mode`: products in safe mode, their checks, the replays run lately, and the
     approvals made in the bad window.
3. **Decide the scope.**
   - One product tripping is usually that product: its lessons or knowledge.
   - Every product tripping on the same phase is usually setup or a model.
   - A drift regression on the pinned cases (the same cases, the same config) is a model or the
     provider, never a product.

| Alert | Likely meaning | Look at first |
| --- | --- | --- |
| `critic_false_pass` | the critic lets weak documents through | the critic's model; lessons marked `check: true` |
| `critic_false_fail` | the critic blocks documents people accept | lessons that over-constrain; critic prompt changes |
| `first_time_approval` drop | writers' documents are worse | the writer role's model; product context growth |
| `escalation_rate` or `stall_rate` | loops that don't converge | stall patterns in `escalation_reasons`; a model change |
| `cost_per_feature` | spend up | prompt growth; a fallback to a dearer model |
| `queue_oldest_minutes` / `external_wait_hours` | stuck work, not quality | workers up? CI or deploys waiting? |
| drift: "Same cases, same config" | the model or provider moved | `served_model` in the ledger; the provider's status page |
| drift: "This week's work" only | the work changed, not the model | product context and lesson growth |

## 2. Contain

**Safe mode does this automatically** for the quality rules and for drift (valxel_sdlc.safe_mode).
While a product is in safe mode, every gate goes to a person:

- no express profile, and G5 for every change
- no batch approval and no quick reads; an approval needs the decision open at least 20 seconds
- a second critic on the tripped phase, with its verdict shown at the gate

Your part:

- **Check that the right products are in.** If the rules missed one, put it in by hand:
  `POST /api/safe-mode/{product}/enter {"note": "why", "phases": ["spec"]}`.
- **Tell the products' approvers.** Every gate page carries the banner, but approvers should know
  why their decisions are slower this week.
- **Cost or stuck-work alerts don't trigger safe mode.** Handle them directly: check the workers,
  the queue and external waits.

## 3. Fix by cause

Fix the cause the triage found. Don't change prompts to chase a model that moved: a prompt change
is a planned migration, with its own evals at the change gate.

| Cause | Fix | How |
| --- | --- | --- |
| **Setup** (a config version went live) | Revert it | Revert the config repo commit. The change gate replays it before it goes live. |
| **Model** (a new snapshot behind a pinned name, or a fallback took over) | Pin the previous snapshot | Set the role's model in `config/models.yaml` to the dated name that `served_model` showed before. Every model is pinned (T-84), so a silent move here is a provider issue: report it. If a fallback took over, fix what pushed the primary off (rate limits, an outage). |
| **Product** (lessons or knowledge) | Withdraw the lesson, or trim the knowledge | Open a PR in the **product's** governance repo removing the lesson under `lessons/`. The product owns its lessons; run `valxel lessons-check` on the change. For prompt growth, trim the product or feature context the product added. |
| **None of these**, pinned cases still worse | Use the fallback model and report it | Switch the role to its configured `fallback` in `config/models.yaml` (through the change gate), and report it to the provider with the drift run's cases and dates. Go back once a replay on the primary is clean. |

When the fix is in, record it so checks judge only the work since then:
`POST /api/safe-mode/{product}/fixed {"note": "Pinned critic to claude-opus-5-5-20261101"}`.

## 4. Verify

Run the checks. Each normal one counts; a bad one starts the count again. Safe mode needs **two
normal checks in a row**.

- **Replay on demand (fast, about an hour):**
  `POST /api/safe-mode/{product}/replay {"phases": ["spec"]}`. Leave out `phases` to replay the
  ones that tripped. This replays the pinned cases for those phases plus the product's own recent
  cases, each phase against its own baseline. It's the only quick check for a model drift, and the
  weekly schedule isn't affected. See the results under `replays` in `GET /api/safe-mode`.
- **Health since the fix (automatic, hourly):** the health job measures the product's work since
  the fix against its baseline from before the bad window. It counts only once each tripped rule
  has its `min_samples` of new decisions and runs, and only when there's new work since the last
  check. A health check can't end a model drift: use a replay or the weekly run for that.
- **The weekly drift run** back at its baseline counts for every product in safe mode. To run it
  now: `POST /api/evals/drift`.

## 5. Close

1. **End safe mode** once two checks in a row are normal: `POST /api/safe-mode/{product}/leave`.
   It must be an owner (VALXEL_EVAL_APPROVERS). It refuses (409) until the checks are in.
2. **Look again at the approvals listed in the reply** (`approvals_to_review`). These are gates
   approved from the start of the bad window until safe mode began. For each one, a product owner
   decides whether it stands or should be reopened. Anything already in production goes through
   the usual incident or change process.
3. **Write it up in the tracker:** the cause, the fix, how long safe mode was on (in the history,
   `safe_mode:history:<product>`), and anything the rules should have caught sooner. Tune
   `config/health.yaml` thresholds through the config repo, never by hand on a cluster.

The tool's own health alerts once when a rule trips, daily while it stays tripped, and once when
it clears. The "back to normal" message for health or drift is a signal, not the end: safe mode
still needs its checks and an owner.
