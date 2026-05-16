variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  description = "Primary GCP region"
  default     = "us-central1"
}

variable "zone" {
  type        = string
  description = "GCP zone for the worker VM"
  default     = "us-central1-a"
}

variable "env" {
  type        = string
  description = "Deployment environment (prod, staging)"
  default     = "prod"
}

variable "github_repo" {
  type        = string
  description = "GitHub repo for WIF OIDC trust (format: owner/repo)"
}

variable "billing_account_id" {
  type        = string
  description = "GCP billing account ID for budget alerts"
}

variable "alert_email" {
  type        = string
  description = "Email for budget and alerting notifications"
}

variable "monthly_budget_usd" {
  type        = number
  description = "Monthly budget threshold in USD"
  default     = 120
}

# ── Image tags (updated by CI/CD, not by Terraform) ──────────────────────────

variable "api_image_tag" {
  type        = string
  description = "Docker image tag for API service"
  default     = "latest"
}

variable "frontend_image_tag" {
  type        = string
  description = "Docker image tag for frontend service"
  default     = "latest"
}

variable "worker_image_tag" {
  type        = string
  description = "Docker image tag for worker service"
  default     = "latest"
}

# ── Secrets ───────────────────────────────────────────────────────────────────
# Secret Manager VALUES are populated out-of-band via bootstrap.sh (not by Terraform).
# Exception: db_password must be in Terraform because it is used to create the
# Cloud SQL user resource. It will appear in state via google_sql_user.app.password
# regardless — this is unavoidable with Terraform-managed Cloud SQL users.

variable "db_password" {
  type        = string
  description = "PostgreSQL master password — used to create the Cloud SQL app user"
  sensitive   = true
}
