output "service_accounts" {
  description = "What Valxel acts as."
  value       = { for k, sa in google_service_account.valxel : k => sa.email }
}

output "next" {
  description = "What to do now."
  value = join("\n", compact([
    "1. Back on Valxel's connections page, enter project ${var.project} and check the connection.",
    var.billing_dataset ? "2. Turn on the Cloud Billing export (a billing account administrator, once): Billing > Billing export > BigQuery export > Standard usage cost > project ${var.project}, dataset valxel_billing. Google has no API for this step; Valxel's cost reviews start from the day it is on." : "",
  ]))
}
