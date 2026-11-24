# Google Cloud fixtures (tracker CP-207)

What Google's services send and return, in their documented shapes, for the GCP adapters' and
parsers' tests (tests/test_cloud_gcp_connect.py). Written from Google's documentation for now;
the nightly cloud-conformance job's `VALXEL_GCP_RECORD=1` run against the sandbox project
replaces the read-only API ones (billing rows, assets) with recorded responses, ids masked.

- `monitoring-incident-open.json`, `monitoring-incident-closed.json`: a Cloud Monitoring webhook
  channel's payload (schema version 1.2)
- `budget-notification.json`: a Cloud Billing budget's Pub/Sub push (message data base64 JSON)
- `scc-finding.json`: a Security Command Center notification's Pub/Sub push
- `billing-rows.json`: rows the billing export queries return
- `assets.json`: Cloud Asset Inventory search results
