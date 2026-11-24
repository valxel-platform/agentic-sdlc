variable "project" {
  description = "Your Google Cloud project, where your products will run."
  type        = string
}

variable "workspace" {
  description = "Your Valxel workspace (from the connections page)."
  type        = string
}

variable "connect_label" {
  description = "A fingerprint of your workspace's external ID (from the connections page)."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.connect_label))
    error_message = "connect_label is the 40 hex characters the connections page gives."
  }
}

variable "mode" {
  description = "lite: your products run on VMs; full: on GKE clusters with managed services."
  type        = string
  validation {
    condition     = contains(["lite", "full"], var.mode)
    error_message = "mode is lite or full."
  }
}

variable "region" {
  description = "Where your products run."
  type        = string
}

variable "valxel_service_account" {
  description = "Valxel's own service account (from the connections page): the only identity that may act as the accounts this makes."
  type        = string
}

variable "billing_dataset" {
  description = "Make the BigQuery dataset for the Cloud Billing export (once per billing account: set false in your other projects)."
  type        = bool
  default     = true
}

variable "billing_location" {
  description = "The billing dataset's location: a multi-region (US, EU) keeps the most history."
  type        = string
  default     = "US"
}
