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

variable "worker_sa_email" {
  type = string
}

variable "vm_sa_email" {
  type        = string
  description = "VM service account email — workers on the VM need GCS read/write"
}

locals {
  prefix = "forensic-flight-${var.env}"
}
