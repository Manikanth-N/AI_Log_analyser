output "api_url" {
  description = "Cloud Run API service URL"
  value       = module.api.api_url
}

output "frontend_url" {
  description = "Cloud Run frontend service URL"
  value       = module.api.frontend_url
}

output "registry_url" {
  description = "Artifact Registry URL prefix for tagging images"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/forensic-flight"
}

output "data_bucket" {
  description = "GCS data bucket name"
  value       = module.storage.data_bucket_name
}

output "vm_name" {
  description = "Worker VM instance name"
  value       = module.compute.vm_name
}

output "vm_zone" {
  description = "Worker VM zone"
  value       = var.zone
}

output "vm_internal_ip" {
  description = "Worker VM static internal IP (Redis + Qdrant host for Cloud Run)"
  value       = module.networking.vm_static_internal_ip
}

output "db_private_ip" {
  description = "Cloud SQL private IP address"
  value       = module.database.db_private_ip
  sensitive   = true
}

output "wif_provider" {
  description = "Workload Identity Federation provider resource name (for GitHub Actions)"
  value       = module.iam.wif_provider_name
}

output "deploy_sa_email" {
  description = "Deployer service account email (for GitHub Actions WIF)"
  value       = module.iam.deploy_sa_email
}
