# ── Secret Manager — containers only ─────────────────────────────────────────
# Terraform creates secret resources and IAM bindings.
# Secret VALUES are populated out-of-band via bootstrap.sh to keep
# plaintext credentials out of Terraform state.

resource "google_secret_manager_secret" "db_password" {
  secret_id = "${local.prefix}-db-password"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "redis_password" {
  secret_id = "${local.prefix}-redis-password"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "api_secret_key" {
  secret_id = "${local.prefix}-api-secret-key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "anthropic_api_key" {
  secret_id = "${local.prefix}-anthropic-api-key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "openai_api_key" {
  secret_id = "${local.prefix}-openai-api-key"
  replication {
    auto {}
  }
}

# ── IAM: API service account ──────────────────────────────────────────────────
# API needs: db password, redis password, app secret key.
# API does NOT call LLMs — workers own inference. No LLM key access granted.

resource "google_secret_manager_secret_iam_member" "api_db_password" {
  secret_id = google_secret_manager_secret.db_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.api_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "api_redis_password" {
  secret_id = google_secret_manager_secret.redis_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.api_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "api_secret_key" {
  secret_id = google_secret_manager_secret.api_secret_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.api_sa_email}"
}

# ── IAM: Worker service account (Cloud Run worker SA — vestigial, kept for future) ──

resource "google_secret_manager_secret_iam_member" "worker_db_password" {
  secret_id = google_secret_manager_secret.db_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.worker_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "worker_redis_password" {
  secret_id = google_secret_manager_secret.redis_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.worker_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "worker_anthropic" {
  secret_id = google_secret_manager_secret.anthropic_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.worker_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "worker_openai" {
  secret_id = google_secret_manager_secret.openai_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.worker_sa_email}"
}

# ── IAM: VM service account — per-secret bindings (not project-level) ────────
# VM SA fetches all 5 secrets at boot to build .env + docker-compose.yml.

resource "google_secret_manager_secret_iam_member" "vm_db_password" {
  secret_id = google_secret_manager_secret.db_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.vm_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "vm_redis_password" {
  secret_id = google_secret_manager_secret.redis_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.vm_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "vm_api_secret_key" {
  secret_id = google_secret_manager_secret.api_secret_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.vm_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "vm_anthropic" {
  secret_id = google_secret_manager_secret.anthropic_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.vm_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "vm_openai" {
  secret_id = google_secret_manager_secret.openai_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.vm_sa_email}"
}
