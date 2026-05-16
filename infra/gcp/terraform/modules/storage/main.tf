# ── GCS Data Bucket ───────────────────────────────────────────────────────────

resource "google_storage_bucket" "data" {
  name                        = "${local.prefix}-data-${var.project_id}"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = false
  }

  lifecycle_rule {
    condition {
      age            = 7
      matches_prefix = ["raw/"]
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      age            = 90
      matches_prefix = ["flights/"]
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  lifecycle_rule {
    condition {
      age            = 365
      matches_prefix = ["reports/"]
    }
    action {
      type          = "SetStorageClass"
      storage_class = "COLDLINE"
    }
  }

  cors {
    origin          = ["*"]
    method          = ["GET", "PUT", "POST"]
    response_header = ["Content-Type", "x-goog-resumable"]
    max_age_seconds = 3600
  }
}

# ── IAM bindings on bucket ────────────────────────────────────────────────────

resource "google_storage_bucket_iam_member" "api_object_admin" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.api_sa_email}"
}

resource "google_storage_bucket_iam_member" "worker_object_admin" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.worker_sa_email}"
}

# VM SA — workers on the VM perform GCS read (raw BIN download) and
# write (parquet upload, report upload). Separate from the Cloud Run worker SA.
resource "google_storage_bucket_iam_member" "vm_object_admin" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.vm_sa_email}"
}
