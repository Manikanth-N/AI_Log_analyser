output "db_password_secret_id" {
  value = google_secret_manager_secret.db_password.id
}

output "redis_password_secret_id" {
  value = google_secret_manager_secret.redis_password.id
}

output "api_secret_key_secret_id" {
  value = google_secret_manager_secret.api_secret_key.id
}

output "anthropic_key_secret_id" {
  value = google_secret_manager_secret.anthropic_api_key.id
}

output "openai_key_secret_id" {
  value = google_secret_manager_secret.openai_api_key.id
}
