output "db_private_ip" {
  value     = google_sql_database_instance.main.private_ip_address
  sensitive = true
}

output "db_name" {
  value = google_sql_database.forensic_flight.name
}

output "db_user" {
  value = google_sql_user.app.name
}

output "instance_name" {
  value = google_sql_database_instance.main.name
}
