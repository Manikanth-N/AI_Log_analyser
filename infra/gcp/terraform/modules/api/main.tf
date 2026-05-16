# ── Cloud Run: API ────────────────────────────────────────────────────────────

resource "google_cloud_run_v2_service" "api" {
  name     = "${local.prefix}-api"
  location = var.region

  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = var.api_sa_email

    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }

    vpc_access {
      connector = var.vpc_connector_id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = "${var.registry_hostname}/${var.registry_repo}/api:${var.api_image_tag}"

      ports {
        container_port = 8000
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle = true
      }

      env {
        name  = "ENV"
        value = "prod"
      }

      env {
        name  = "LOG_LEVEL"
        value = "INFO"
      }

      env {
        name  = "GCS_DATA_BUCKET"
        value = var.gcs_data_bucket
      }

      env {
        name  = "REDIS_HOST"
        value = var.redis_host
      }

      env {
        name  = "REDIS_PORT"
        value = "6379"
      }

      env {
        name  = "DB_NAME"
        value = var.db_name
      }

      env {
        name  = "DB_HOST"
        value = var.db_host
      }

      # Secrets injected at runtime from Secret Manager
      env {
        name = "DB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.db_password_secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "REDIS_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.redis_password_secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "SECRET_KEY"
        value_source {
          secret_key_ref {
            secret  = var.api_secret_key_secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        http_get {
          path = "/health"
          port = 8000
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 10
      }

      liveness_probe {
        http_get {
          path = "/health"
          port = 8000
        }
        period_seconds    = 30
        failure_threshold = 3
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
    ]
  }
}

# Allow unauthenticated invocations for the API (auth handled at app layer via JWT)
resource "google_cloud_run_v2_service_iam_member" "api_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ── Cloud Run: Frontend (nginx serving React SPA) ─────────────────────────────

resource "google_cloud_run_v2_service" "frontend" {
  name     = "${local.prefix}-frontend"
  location = var.region

  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = var.frontend_sa_email

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      image = "${var.registry_hostname}/${var.registry_repo}/frontend:${var.frontend_image_tag}"

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "0.5"
          memory = "256Mi"
        }
        cpu_idle = true
      }

      env {
        name  = "API_URL"
        value = google_cloud_run_v2_service.api.uri
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_service_iam_member" "frontend_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.frontend.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
