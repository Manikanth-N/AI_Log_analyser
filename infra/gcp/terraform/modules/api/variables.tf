variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "env" {
  type = string
}

variable "api_sa_email" {
  type = string
}

variable "frontend_sa_email" {
  type        = string
  description = "Dedicated SA for the nginx frontend — no GCP permissions attached"
}

variable "vpc_connector_id" {
  type = string
}

variable "db_host" {
  type      = string
  sensitive = true
}

variable "db_name" {
  type = string
}

variable "redis_host" {
  type        = string
  description = "VM static internal IP for Redis"
}

variable "gcs_data_bucket" {
  type = string
}

variable "registry_hostname" {
  type = string
}

variable "registry_repo" {
  type = string
}

variable "api_image_tag" {
  type    = string
  default = "latest"
}

variable "frontend_image_tag" {
  type    = string
  default = "latest"
}

variable "db_password_secret_id" {
  type = string
}

variable "redis_password_secret_id" {
  type = string
}

variable "api_secret_key_secret_id" {
  type = string
}

locals {
  prefix = "forensic-flight-${var.env}"
}
