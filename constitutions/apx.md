---
name: apx
version: 1.0.0
---
# APX payment platform constitution

Starter draft. Regulatory articles need compliance counsel review before they are treated as complete.

- **APX-1** A double-entry ledger is the source of truth: journals are immutable and balances are derived. (Test)
- **APX-2** Every money-moving API takes an idempotency key and has an exactly-once effect. (Test)
- **APX-3** Rail rules are encoded and tested: NACHA for ACH, network rules for RTP and cards. (Test)
- **APX-4** PCI DSS scope is minimized: card numbers are tokenized at the edge. (Policy)
- **APX-5** Reconciliation runs daily per rail; an unexplained break blocks the next release. (Test)
- **APX-6** APIs and events are versioned and backward compatible, with announced deprecation windows. (Test)
- **APX-7** Fraud and risk decisions are explainable and logged with their inputs. (Rubric, Test)
- **APX-8** Any change to money-movement logic goes through G5, regardless of risk score. (Policy)
