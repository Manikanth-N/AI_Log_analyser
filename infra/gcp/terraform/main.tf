terraform {
  required_version = ">= 1.7"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 5.0"
    }
  }

  # GCS backend — bucket must be created manually before first init
  # gsutil mb -l us-central1 gs://forensic-flight-tfstate
  backend "gcs" {
    bucket = "forensic-flight-tfstate"
    prefix = "terraform/state"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

# ── Enable required GCP APIs ──────────────────────────────────────────────────

locals {
  required_apis = [
    "run.googleapis.com",
    "compute.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "vpcaccess.googleapis.com",
    "servicenetworking.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iap.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "billingbudgets.googleapis.com",
    "pubsub.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.required_apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ── Artifact Registry ─────────────────────────────────────────────────────────

resource "google_artifact_registry_repository" "main" {
  provider = google

  location      = var.region
  repository_id = "forensic-flight"
  format        = "DOCKER"
  description   = "Forensic Flight container images"

  depends_on = [google_project_service.apis]
}

# ── Modules ───────────────────────────────────────────────────────────────────

module "networking" {
  source = "./modules/networking"

  project_id = var.project_id
  region     = var.region
  env        = var.env

  depends_on = [google_project_service.apis]
}

module "iam" {
  source = "./modules/iam"

  project_id  = var.project_id
  env         = var.env
  github_repo = var.github_repo

  depends_on = [google_project_service.apis]
}

module "storage" {
  source = "./modules/storage"

  project_id      = var.project_id
  region          = var.region
  env             = var.env
  api_sa_email    = module.iam.api_sa_email
  worker_sa_email = module.iam.worker_sa_email
  vm_sa_email     = module.iam.vm_sa_email

  depends_on = [module.iam]
}

module "secrets" {
  source = "./modules/secrets"

  project_id      = var.project_id
  env             = var.env
  api_sa_email    = module.iam.api_sa_email
  worker_sa_email = module.iam.worker_sa_email
  vm_sa_email     = module.iam.vm_sa_email

  depends_on = [module.iam, google_project_service.apis]
}

module "database" {
  source = "./modules/database"

  project_id                    = var.project_id
  region                        = var.region
  env                           = var.env
  db_password                   = var.db_password
  vpc_id                        = module.networking.vpc_id
  private_ip_peering_dependency = module.networking.private_ip_peering

  depends_on = [module.networking, google_project_service.apis]
}

module "compute" {
  source = "./modules/compute"

  project_id               = var.project_id
  region                   = var.region
  zone                     = var.zone
  env                      = var.env
  vm_sa_email              = module.iam.vm_sa_email
  subnet_id                = module.networking.workers_subnet_id
  static_ip_address        = module.networking.vm_static_internal_ip
  redis_password_secret_id = module.secrets.redis_password_secret_id
  db_password_secret_id    = module.secrets.db_password_secret_id
  api_secret_key_secret_id = module.secrets.api_secret_key_secret_id
  anthropic_key_secret_id  = module.secrets.anthropic_key_secret_id
  openai_key_secret_id     = module.secrets.openai_key_secret_id
  db_host                  = module.database.db_private_ip
  db_name                  = module.database.db_name
  gcs_data_bucket          = module.storage.data_bucket_name
  registry_hostname        = "${var.region}-docker.pkg.dev"
  registry_repo            = "${var.project_id}/forensic-flight"
  image_tag                = var.worker_image_tag

  depends_on = [module.networking, module.iam, module.secrets, module.database, module.storage]
}

module "api" {
  source = "./modules/api"

  project_id               = var.project_id
  region                   = var.region
  env                      = var.env
  api_sa_email             = module.iam.api_sa_email
  frontend_sa_email        = module.iam.frontend_sa_email
  vpc_connector_id         = module.networking.vpc_connector_id
  db_host                  = module.database.db_private_ip
  db_name                  = module.database.db_name
  redis_host               = module.networking.vm_static_internal_ip
  gcs_data_bucket          = module.storage.data_bucket_name
  registry_hostname        = "${var.region}-docker.pkg.dev"
  registry_repo            = "${var.project_id}/forensic-flight"
  api_image_tag            = var.api_image_tag
  frontend_image_tag       = var.frontend_image_tag
  db_password_secret_id    = module.secrets.db_password_secret_id
  redis_password_secret_id = module.secrets.redis_password_secret_id
  api_secret_key_secret_id = module.secrets.api_secret_key_secret_id

  depends_on = [module.networking, module.iam, module.secrets, module.database, module.compute, module.storage]
}

module "monitoring" {
  source = "./modules/monitoring"

  project_id         = var.project_id
  env                = var.env
  alert_email        = var.alert_email
  monthly_budget_usd = var.monthly_budget_usd
  billing_account_id = var.billing_account_id
  api_service_name   = module.api.api_service_name

  depends_on = [module.api, google_project_service.apis]
}
