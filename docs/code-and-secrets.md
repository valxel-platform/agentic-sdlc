# Where the hosted tool may keep customer code or secrets (T-121)

For hosted sessions the agreement says customer code is handled only while working, and not kept.
This is every place the tool writes something that could hold customer code, a fragment of it, or
a secret, with how long it lasts and what deletes it. Each row names the code or test that proves it.

Secrets get one rule everywhere: none are stored outside the secret store and KMS. Anything the
tool keeps that came from an agent or a build (state, notes, eval cases) is passed through
`secret_scrub` on the way in, which masks the shapes the gateway redacts (GitHub tokens, AWS keys,
model API keys, JWTs, private key blocks) to their first four characters.

| Place | What may be there | How long | What deletes it | Evidence |
|---|---|---|---|---|
| Session folders and read-only checkouts (sandbox) | The repository, the session's short-lived GitHub token | The session | Removed in `finally` when the session ends; anything a crashed worker left is swept after 6 hours by the hourly `sweep_sandboxes` job | `sandbox.py` (`sweep`), `session_runner.py`; `tests/test_secret_scrub.py::test_leftover_session_folders_and_checkouts_are_swept` |
| Agent session transcripts | Prompts, tool output, code the agent read | 24 hours (long enough to resume) | Hourly `expire_transcripts` job | `sessions.py` (`RESUME_WINDOW_S`), `engine_parts/watching.py` |
| Feature state and checkpoints (Postgres) | Agents' free text: summaries, handoffs, findings, test failure messages, CI failures. Code in them is cut to short excerpts (see below); secrets are masked at write time | As long as the feature; only the latest checkpoint is kept after each run | Deleted with the feature. Regulated lanes: see the next row | `api/app.py`, `cli.py` (`ScrubbingSerde`), `engine.py` (`_prune`); `tests/test_secret_scrub.py::test_a_features_stored_history_holds_no_secret` |
| Regulated-lane checkpoint archive (S3, Object Lock) | Every step's state for phi and payments lanes: the same free text, code excerpts only, secrets masked | 6 years (`VALXEL_CHECKPOINT_ARCHIVE_RETENTION_DAYS`, 2190) | Object Lock expiry; no one can delete earlier, the tool included | `checkpoint_archive.py` |
| Coder phase summary | The agent's reply summary, or the first 2000 characters of its output when there is no structured reply | With the feature state | As above | `graph/phase.py` |
| Eval cases (Postgres) | Document phases: their context and the documents they wrote. Independent reviewer: the PR's title and body. Code phases keep no code. Secrets masked | Unlabeled 90 days, labeled 730 days (`VALXEL_EVAL_CASE_RETENTION_DAYS`, `VALXEL_EVAL_LABELED_RETENTION_DAYS`) | Daily `prune_eval_cases` job | `evals.py` (`prune`); `tests/test_secret_scrub.py::test_eval_cases_*` |
| Cluster notes (Postgres) | Short notes from watchers and builds (errors, step names). Secrets masked | 30 days | Hourly `prune_cluster_notes` job | `cluster.py`; `tests/test_secret_scrub.py::test_notes_and_eval_cases_in_postgres_hold_no_secret` |
| Drafts | Documents a feature wrote that were not approved | 30 days after the feature is finished (`VALXEL_DRAFT_RETENTION_DAYS`) | Daily `remove_drafts` job | `drafts.py` |
| Cost ledger | Metadata only: tokens, cost, feature and phase ids | Kept | Not code; no deletion needed | `gateway/ledger.py`, `finops/ledger.py` |
| Audit trail and anchors | Ids, actors, decisions, hashes. No content | 6 years under Object Lock | Object Lock expiry | `audit_events.py`, `audit_anchors.py` |
| The product's own repository | Approved documents only; never transcripts, prompts or evaluator notes | The customer's | The customer | `records.py` |
| Model providers | Prompts and responses sent through the gateway | The provider's policy | The provider (zero-retention terms where offered) | Gateway configuration |

## Code in agents' notes: excerpts only (decided 2026-11-22)

Agents' notes can quote code: a failing test's message, a finding that cites a line. The feature's
history keeps only a short excerpt of it, never a whole file: in the note channels (`diaries`,
`findings`, `escalations`, `events`, `ci_failures`, `task_results`, `reviews`, `reason`) each fenced
code block keeps its first 20 lines and each note at most 4,000 characters. Secrets in them are
masked as everywhere else. Documents the agents write are not notes and are kept whole; approved
ones live in the customer's repository.

These excerpts stay with the feature, and for regulated lanes in the six-year archive, as part of
the audit record. The customer agreement says so.

Evidence: `secret_scrub.py` (`keep_excerpts`, `excerpt`), wrapped around every checkpointer in
`api/app.py` and `cli.py`; `tests/test_secret_scrub.py::test_notes_keep_an_excerpt_of_code_never_the_whole_of_it`,
`test_a_features_history_keeps_excerpts_in_notes_and_documents_whole`. The test suite's engine uses
the same saver.
