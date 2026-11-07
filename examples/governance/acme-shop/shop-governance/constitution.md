---
name: shop
version: 1.0.0
---
# SHOP constitution

Starter draft. Regulatory articles need compliance counsel review before they are treated as complete.

- **SHOP-1** Card data never touches SHOP systems; capture uses PAY tokenization to keep SHOP out of PCI scope. (Policy, Test)
- **SHOP-2** Offline mode stores and forwards transactions and replays them idempotently. (Test)
- **SHOP-3** A p95 checkout-latency budget is a release gate. (Test)
- **SHOP-4** Printers, scanners and card readers sit behind a hardware abstraction layer. (Rubric)
- **SHOP-5** Every money movement posts through the PAY ledger; SHOP keeps no balances of its own. (Policy)
- **SHOP-6** Merchant data is isolated per tenant at the query and storage layers. (Test)
