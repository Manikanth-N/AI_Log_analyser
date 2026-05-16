output "api_url" {
  value = google_cloud_run_v2_service.api.uri
}

output "frontend_url" {
  value = google_cloud_run_v2_service.frontend.uri
}

output "api_service_name" {
  value = google_cloud_run_v2_service.api.name
}
