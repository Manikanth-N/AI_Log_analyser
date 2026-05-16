variable "project_id" {
  type = string
}

variable "env" {
  type = string
}

variable "alert_email" {
  type = string
}

variable "monthly_budget_usd" {
  type    = number
  default = 120
}

variable "billing_account_id" {
  type = string
}

variable "api_service_name" {
  type = string
}
