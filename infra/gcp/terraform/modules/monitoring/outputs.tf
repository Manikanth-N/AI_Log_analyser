output "notification_channel_id" {
  value = google_monitoring_notification_channel.email.id
}

output "budget_name" {
  value = google_billing_budget.main.display_name
}
