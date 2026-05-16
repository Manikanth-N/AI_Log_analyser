output "api_sa_email" {
  value = google_service_account.api.email
}

output "frontend_sa_email" {
  value = google_service_account.frontend.email
}

output "worker_sa_email" {
  value = google_service_account.worker.email
}

output "vm_sa_email" {
  value = google_service_account.vm.email
}

output "deploy_sa_email" {
  value = google_service_account.deploy.email
}

output "wif_provider_name" {
  value = google_iam_workload_identity_pool_provider.github_oidc.name
}
