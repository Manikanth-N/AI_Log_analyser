# ── Persistent disk for Qdrant data ──────────────────────────────────────────

resource "google_compute_disk" "qdrant" {
  name = "${local.prefix}-qdrant-data"
  type = "pd-ssd"
  zone = var.zone
  size = 20
}

# ── Worker VM ─────────────────────────────────────────────────────────────────

resource "google_compute_instance" "worker" {
  name         = "${local.prefix}-worker"
  machine_type = "e2-standard-2"
  zone         = var.zone

  tags = ["forensic-flight-worker"]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 30
      type  = "pd-ssd"
    }
  }

  attached_disk {
    source      = google_compute_disk.qdrant.self_link
    device_name = "qdrant-data"
    mode        = "READ_WRITE"
  }

  network_interface {
    subnetwork = var.subnet_id
    network_ip = var.static_ip_address

    # External IP for LLM API egress; all ingress blocked by firewall
    access_config {}
  }

  service_account {
    email  = var.vm_sa_email
    scopes = ["cloud-platform"]
  }

  metadata = {
    enable-oslogin = "TRUE"
  }

  metadata_startup_script = templatefile("${path.module}/startup.sh.tpl", {
    redis_password_secret_id_short = split("/", var.redis_password_secret_id)[3]
    db_password_secret_id_short    = split("/", var.db_password_secret_id)[3]
    api_secret_key_secret_id_short = split("/", var.api_secret_key_secret_id)[3]
    anthropic_key_secret_id_short  = split("/", var.anthropic_key_secret_id)[3]
    openai_key_secret_id_short     = split("/", var.openai_key_secret_id)[3]
    db_host                        = var.db_host
    db_name                        = var.db_name
    gcs_data_bucket                = var.gcs_data_bucket
    registry_hostname              = var.registry_hostname
    registry_repo                  = var.registry_repo
    image_tag                      = var.image_tag
  })

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }
}
