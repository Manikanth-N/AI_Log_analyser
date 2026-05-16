# ── VPC ───────────────────────────────────────────────────────────────────────

resource "google_compute_network" "main" {
  name                    = "${local.prefix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

# ── Subnets ───────────────────────────────────────────────────────────────────

resource "google_compute_subnetwork" "workers" {
  name                     = "${local.prefix}-workers"
  ip_cidr_range            = "10.0.1.0/24"
  region                   = var.region
  network                  = google_compute_network.main.id
  private_ip_google_access = true
}

# ── Private IP range for Cloud SQL VPC peering ────────────────────────────────

resource "google_compute_global_address" "sql_private_range" {
  name          = "${local.prefix}-sql-private-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = google_compute_network.main.id
}

resource "google_service_networking_connection" "sql_peering" {
  network                 = google_compute_network.main.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.sql_private_range.name]
}

# ── Serverless VPC Connector (Cloud Run → private VPC) ───────────────────────

resource "google_vpc_access_connector" "main" {
  provider = google-beta

  name          = "${local.prefix}-conn"
  region        = var.region
  network       = google_compute_network.main.name
  ip_cidr_range = "10.0.8.0/28"
  min_instances = 2
  max_instances = 5
  machine_type  = "e2-micro"
  max_throughput = 500
}

# ── Static internal IP for VM (deterministic Redis host for Cloud Run) ────────

resource "google_compute_address" "vm_internal" {
  name         = "${local.prefix}-vm-internal"
  address_type = "INTERNAL"
  subnetwork   = google_compute_subnetwork.workers.id
  region       = var.region
  address      = "10.0.1.10"
}

# ── Cloud NAT (VM egress to LLM APIs without public IP per-interface) ─────────
# VM has an external IP for egress; NAT not strictly needed but left as option.
# Firewall rules block all ingress — external IP is egress-only in practice.

# ── Firewall rules ────────────────────────────────────────────────────────────

# IAP SSH — allows Google's IAP proxy to reach the VM on port 22
resource "google_compute_firewall" "allow_iap_ssh" {
  name    = "${local.prefix}-allow-iap-ssh"
  network = google_compute_network.main.name

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  # IAP source range — https://cloud.google.com/iap/docs/using-tcp-forwarding
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["forensic-flight-worker"]
}

# Redis from VPC Connector subnet only
resource "google_compute_firewall" "allow_redis_from_connector" {
  name    = "${local.prefix}-allow-redis-connector"
  network = google_compute_network.main.name

  allow {
    protocol = "tcp"
    ports    = ["6379"]
  }

  source_ranges = ["10.0.8.0/28"]
  target_tags   = ["forensic-flight-worker"]
}

# Internal VPC traffic (workers subnet → VM) — needed for Celery workers on VM
resource "google_compute_firewall" "allow_internal" {
  name    = "${local.prefix}-allow-internal"
  network = google_compute_network.main.name

  allow {
    protocol = "tcp"
    ports    = ["0-65535"]
  }

  allow {
    protocol = "udp"
    ports    = ["0-65535"]
  }

  source_ranges = ["10.0.1.0/24"]
  target_tags   = ["forensic-flight-worker"]
}

# Deny all other ingress (explicit — belt-and-suspenders)
resource "google_compute_firewall" "deny_all_ingress" {
  name      = "${local.prefix}-deny-all-ingress"
  network   = google_compute_network.main.name
  priority  = 65534
  direction = "INGRESS"

  deny {
    protocol = "all"
  }

  source_ranges = ["0.0.0.0/0"]
}
