output "vm_name" {
  value = google_compute_instance.worker.name
}

output "vm_self_link" {
  value = google_compute_instance.worker.self_link
}

output "vm_internal_ip" {
  value = google_compute_instance.worker.network_interface[0].network_ip
}
