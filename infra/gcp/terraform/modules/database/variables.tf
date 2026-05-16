variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "env" {
  type = string
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "vpc_id" {
  type        = string
  description = "VPC network self-link for private IP peering"
}

variable "private_ip_peering_dependency" {
  description = "Dependency on the service_networking_connection to ensure peering is established"
}

locals {
  prefix = "forensic-flight-${var.env}"
}
