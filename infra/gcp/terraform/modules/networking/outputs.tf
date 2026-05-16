output "vpc_id" {
  value = google_compute_network.main.id
}

output "vpc_name" {
  value = google_compute_network.main.name
}

output "workers_subnet_id" {
  value = google_compute_subnetwork.workers.id
}

output "workers_subnet_name" {
  value = google_compute_subnetwork.workers.name
}

output "vpc_connector_id" {
  value = google_vpc_access_connector.main.id
}

output "vm_static_internal_ip" {
  value = google_compute_address.vm_internal.address
}

output "private_ip_peering" {
  value = google_service_networking_connection.sql_peering
}
