variable "project_id" {
  type = string
}

variable "env" {
  type = string
}

variable "github_repo" {
  type        = string
  description = "GitHub repo in owner/name format"
}

locals {
  prefix = "forensic-flight-${var.env}"
}
