output "service_accounts" {
  description = "What Valxel acts as."
  value       = { for k, sa in google_service_account.valxel : k => sa.email }
}

output "platform" {
  description = "Full mode: the data plane's accounts and its state bucket (Valxel reads these when it checks the connection)."
  value = var.mode == "full" ? {
    state_bucket     = google_storage_bucket.state[0].name
    service_accounts = { for k, sa in google_service_account.platform : replace(k, "-", "_") => sa.email }
  } : null
}

output "next" {
  description = "What to do now."
  value = join("\n", compact([
    "1. Back on Valxel's connections page, enter project ${var.project} and check the connection.",
    var.billing_dataset ? "2. Turn on the Cloud Billing export (a billing account administrator, once): Billing > Billing export > BigQuery export > Standard usage cost > project ${var.project}, dataset valxel_billing. Google has no API for this step; Valxel's cost reviews start from the day it is on." : "",
  ]))
}
