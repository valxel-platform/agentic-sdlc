# Connects a Google Cloud project to a Valxel workspace (agentic-sdlc ADR 0010; tracker CP-203): the
# counterpart of deploy/customer-stack.yaml on AWS. Run it from Valxel's connections page, which
# opens Cloud Shell here and gives the command with your workspace's values.
#
# It makes two service accounts Valxel may impersonate, 15 minutes at a time, for one job:
#   valxel-deploy  creates and runs your products: VMs in lite mode, GKE clusters and the catalog's
#                  managed services in full mode. Its roles are project-wide (Google Cloud scopes
#                  most roles to a project, not a label), so give Valxel projects of its own.
#   valxel-agents  reads resource metadata, labels, alerts, findings and costs; no data, no changes
# Each one's description names your workspace and a fingerprint of its external ID, which Valxel
# checks: another workspace can't use them.
#
# Full mode also makes what your products' data plane (a GKE cluster Valxel builds in this project at
# the product gate) runs as, each with only its own roles, granted here by you: valxel-deploy can bind
# these to the cluster but can't give any account a project role.
#   valxel-nodes             the cluster's nodes: logs, metrics, image pulls
#   valxel-crossplane        builds the catalog's services (Cloud SQL, Pub/Sub, Firestore, Storage) and
#                            may grant your products' accounts only the catalog's Firestore roles
#   valxel-db-logins         each product database's IAM admin: no database password anywhere
#   valxel-cert-manager      proves the plane's domain to Let's Encrypt (Cloud DNS)
#   valxel-external-secrets  reads your products' secrets in Secret Manager
# and a bucket for the data plane's OpenTofu state, which stays in this project. Billing data: a BigQuery dataset for the Cloud Billing
# export, which you turn on once in the console (Google has no API for that step).

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 7.0" }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

locals {
  description = "valxel workspace=${var.workspace} connect=${var.connect_label}"
  valxel      = "serviceAccount:${var.valxel_service_account}"
  deploy_roles = var.mode == "lite" ? [
    "roles/compute.instanceAdmin.v1", "roles/compute.securityAdmin", "roles/iam.serviceAccountUser",
    ] : [
    "roles/container.admin", "roles/compute.networkAdmin", "roles/iam.serviceAccountUser",
    "roles/pubsub.admin", "roles/datastore.owner", "roles/storage.admin", "roles/managedkafka.admin",
    "roles/monitoring.alertPolicyEditor", "roles/iam.serviceAccountAdmin",
    # The data plane: its network, firewall, keys, DNS zone and private service access.
    "roles/compute.securityAdmin", "roles/cloudkms.admin", "roles/dns.admin",
    "roles/servicenetworking.networksAdmin", "roles/serviceusage.serviceUsageConsumer",
  ]
  agents_roles = ["roles/cloudasset.viewer", "roles/monitoring.viewer", "roles/logging.viewer",
  "roles/browser", "roles/securitycenter.findingsViewer"]
  apis = concat(["iam.googleapis.com", "iamcredentials.googleapis.com", "cloudasset.googleapis.com",
    "monitoring.googleapis.com", "bigquery.googleapis.com", "compute.googleapis.com",
    "container.googleapis.com", "cloudresourcemanager.googleapis.com"], var.mode == "full" ? [
    "sqladmin.googleapis.com", "servicenetworking.googleapis.com", "cloudkms.googleapis.com",
    "dns.googleapis.com", "secretmanager.googleapis.com", "pubsub.googleapis.com", "firestore.googleapis.com",
    "storage.googleapis.com", "artifactregistry.googleapis.com", "logging.googleapis.com",
    "binaryauthorization.googleapis.com",
  ] : [])

  # Full mode: the data plane's own accounts and their roles (see the top of this file).
  platform = var.mode == "full" ? {
    nodes = ["roles/logging.logWriter", "roles/monitoring.metricWriter", "roles/monitoring.viewer",
    "roles/artifactregistry.reader"]
    crossplane = ["roles/cloudsql.admin", "roles/pubsub.admin", "roles/datastore.owner", "roles/storage.admin",
      "roles/monitoring.alertPolicyEditor", "roles/monitoring.notificationChannelEditor",
    "roles/iam.serviceAccountAdmin"]
    db-logins        = ["roles/cloudsql.client", "roles/cloudsql.instanceUser"]
    cert-manager     = ["roles/dns.admin"]
    external-secrets = ["roles/secretmanager.secretAccessor"]
  } : {}
  platform_grants = merge([for name, roles in local.platform : { for r in roles : "${name} ${r}" => { name = name, role = r } }]...)
  # The only project roles Crossplane may grant: what the catalog gives a product's account on its
  # Firestore database (platform-resources gcp/key-value-table-composition.yaml).
  catalog_project_roles = ["roles/datastore.user", "roles/datastore.viewer"]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "valxel" {
  for_each     = toset(["deploy", "agents"])
  account_id   = "valxel-${each.value}"
  display_name = "Valxel ${each.value} (${var.workspace})"
  description  = local.description
  depends_on   = [google_project_service.apis]
}

# Valxel may act as each account, and each account may read its own record (Valxel's check).
resource "google_service_account_iam_member" "valxel_impersonates" {
  for_each           = google_service_account.valxel
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.valxel
}

resource "google_service_account_iam_member" "reads_itself" {
  for_each           = google_service_account.valxel
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountViewer"
  member             = "serviceAccount:${each.value.email}"
}

resource "google_project_iam_member" "deploy" {
  for_each = toset(local.deploy_roles)
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.valxel["deploy"].email}"
}

resource "google_project_iam_member" "agents" {
  for_each = toset(local.agents_roles)
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.valxel["agents"].email}"
}

# Billing data (CostSource on GCP): the export's dataset, readable by the agents account and by
# Valxel's own identity. Turn the export on in the console afterwards (the output says how).
resource "google_bigquery_dataset" "billing" {
  count                      = var.billing_dataset ? 1 : 0
  dataset_id                 = "valxel_billing"
  location                   = var.billing_location
  description                = "Cloud Billing export for Valxel's cost reviews (turned on in the console)"
  delete_contents_on_destroy = false
  depends_on                 = [google_project_service.apis]
}

resource "google_bigquery_dataset_iam_member" "billing_readers" {
  for_each   = var.billing_dataset ? toset(["serviceAccount:${google_service_account.valxel["agents"].email}", local.valxel]) : toset([])
  dataset_id = google_bigquery_dataset.billing[0].dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = each.value
}

resource "google_project_iam_member" "billing_jobs" {
  for_each = var.billing_dataset ? toset(["serviceAccount:${google_service_account.valxel["agents"].email}", local.valxel]) : toset([])
  project  = var.project
  role     = "roles/bigquery.jobUser"
  member   = each.value
}

# ---------------------------------------------------------------- full mode: the data plane's accounts

resource "google_service_account" "platform" {
  for_each     = local.platform
  account_id   = "valxel-${each.key}"
  display_name = "Valxel data plane: ${each.key} (${var.workspace})"
  description  = local.description
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "platform" {
  for_each = local.platform_grants
  project  = var.project
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.platform[each.value.name].email}"
}

resource "google_project_iam_member" "crossplane_grants" {
  count   = var.mode == "full" ? 1 : 0
  project = var.project
  role    = "roles/resourcemanager.projectIamAdmin"
  member  = "serviceAccount:${google_service_account.platform["crossplane"].email}"
  condition {
    title       = "only the catalog's Firestore roles"
    description = "Crossplane may grant or remove these project roles and no other"
    expression  = "api.getAttribute('iam.googleapis.com/modifiedGrantsByRole', []).hasOnly(${jsonencode(local.catalog_project_roles)})"
  }
}

# The data plane's OpenTofu state, in this project: versioned, never public.
resource "google_storage_bucket" "state" {
  count                       = var.mode == "full" ? 1 : 0
  name                        = "${var.project}-valxel-state"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning {
    enabled = true
  }
  lifecycle_rule {
    condition {
      num_newer_versions = 20
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "state" {
  count  = var.mode == "full" ? 1 : 0
  bucket = google_storage_bucket.state[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.valxel["deploy"].email}"
}
