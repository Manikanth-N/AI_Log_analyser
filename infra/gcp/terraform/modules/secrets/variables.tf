variable "project_id" {
  type = string
}

variable "env" {
  type = string
}

variable "api_sa_email" {
  type = string
}

variable "worker_sa_email" {
  type = string
}

variable "vm_sa_email" {
  type = string
}

locals {
  prefix = "forensic-flight-${var.env}"
}
