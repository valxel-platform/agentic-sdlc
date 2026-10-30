---
name: pos
version: 1.0.0
---
# POS constitution

Starter draft. Regulatory articles need compliance counsel review before they are treated as complete.

- **POS-1** Card data never touches POS systems; capture uses APX tokenization to keep POS out of PCI scope. (Policy, Test)
- **POS-2** Offline mode stores and forwards transactions and replays them idempotently. (Test)
- **POS-3** A p95 checkout-latency budget is a release gate. (Test)
- **POS-4** Printers, scanners and card readers sit behind a hardware abstraction layer. (Rubric)
- **POS-5** Every money movement posts through the APX ledger; POS keeps no balances of its own. (Policy)
- **POS-6** Merchant data is isolated per tenant at the query and storage layers. (Test)
