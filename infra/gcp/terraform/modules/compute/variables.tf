variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "zone" {
  type = string
}

variable "env" {
  type = string
}

variable "vm_sa_email" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "static_ip_address" {
  type        = string
  description = "Static internal IP reserved for the VM (e.g. 10.0.1.10)"
}

variable "redis_password_secret_id" {
  type = string
}

variable "db_password_secret_id" {
  type = string
}

variable "api_secret_key_secret_id" {
  type = string
}

variable "anthropic_key_secret_id" {
  type = string
}

variable "openai_key_secret_id" {
  type = string
}

variable "db_host" {
  type      = string
  sensitive = true
}

variable "db_name" {
  type = string
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

variable "image_tag" {
  type    = string
  default = "latest"
}

locals {
  prefix = "forensic-flight-${var.env}"
}
