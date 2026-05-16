# ── Service Accounts ──────────────────────────────────────────────────────────

resource "google_service_account" "api" {
  account_id   = "${local.prefix}-api"
  display_name = "Forensic Flight API (Cloud Run)"
}

resource "google_service_account" "worker" {
  account_id   = "${local.prefix}-worker"
  display_name = "Forensic Flight Worker (Cloud Run)"
}

resource "google_service_account" "vm" {
  account_id   = "${local.prefix}-vm"
  display_name = "Forensic Flight VM (Compute Engine)"
}

resource "google_service_account" "frontend" {
  account_id   = "${local.prefix}-frontend"
  display_name = "Forensic Flight Frontend (Cloud Run nginx)"
  # No IAM bindings — nginx serves static files and needs zero GCP permissions.
  # Exists so the frontend service does not inherit the API SA's GCS/Secret access.
}

resource "google_service_account" "deploy" {
  account_id   = "${local.prefix}-deploy"
  display_name = "Forensic Flight GitHub Actions Deployer"
}

# ── VM service account IAM ────────────────────────────────────────────────────
# VM needs: logging, monitoring, AR reader.
# Secret Manager access is granted per-secret in the secrets module — NOT at
# project level, which would allow reading any secret in the project.

resource "google_project_iam_member" "vm_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_project_iam_member" "vm_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_project_iam_member" "vm_ar_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

# ── Deploy service account IAM ────────────────────────────────────────────────
# Deployer needs: Cloud Run developer, AR writer, Secret Manager viewer,
#                 compute instance updater (for startup-script updates),
#                 iam.serviceAccountUser (to act-as worker/api SAs)

resource "google_project_iam_member" "deploy_run_developer" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_project_iam_member" "deploy_ar_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_project_iam_member" "deploy_compute_instance_admin" {
  project = var.project_id
  role    = "roles/compute.instanceAdmin.v1"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# IAP TCP tunnel — required for `gcloud compute ssh --tunnel-through-iap` in CI
resource "google_project_iam_member" "deploy_iap_tunnel" {
  project = var.project_id
  role    = "roles/iap.tunnelResourceAccessor"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# OS Login — required to authenticate SSH sessions via service account identity
resource "google_project_iam_member" "deploy_os_login" {
  project = var.project_id
  role    = "roles/compute.osLogin"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# Allow deployer to act-as VM, API, worker service accounts
resource "google_service_account_iam_member" "deploy_act_as_vm" {
  service_account_id = google_service_account.vm.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_service_account_iam_member" "deploy_act_as_api" {
  service_account_id = google_service_account.api.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_service_account_iam_member" "deploy_act_as_worker" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deploy.email}"
}

# ── Workload Identity Federation ──────────────────────────────────────────────

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "${local.prefix}-github"
  display_name              = "GitHub Actions"
  description               = "WIF pool for GitHub Actions CI/CD"
}

resource "google_iam_workload_identity_pool_provider" "github_oidc" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  display_name                       = "GitHub OIDC"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.actor"      = "assertion.actor"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Scope trust to main branch of this specific repo
  attribute_condition = "assertion.repository == '${var.github_repo}' && assertion.ref == 'refs/heads/main'"
}

# Allow GitHub Actions (via WIF) to impersonate the deploy service account
resource "google_service_account_iam_member" "wif_deploy" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}
