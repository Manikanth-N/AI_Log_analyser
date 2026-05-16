variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "env" {
  type = string
}

locals {
  prefix = "forensic-flight-${var.env}"
}
