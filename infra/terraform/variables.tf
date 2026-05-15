variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "image_tag" {
  type        = string
  description = "Docker image tag to deploy (set by CI/CD, e.g. git SHA)"
  default     = "latest"
}

# ── Networking ────────────────────────────────────────────────────────────────

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "availability_zones" {
  type    = list(string)
  default = ["us-east-1a", "us-east-1b"]
}

# ── Domain ────────────────────────────────────────────────────────────────────

variable "domain_name" {
  type        = string
  description = "Root domain name (e.g. forensicflight.io). Leave empty to skip Route53/ACM."
  default     = ""
}

variable "api_subdomain" {
  type    = string
  default = "api"
}

variable "acm_certificate_arn" {
  type        = string
  description = "ACM certificate ARN for the ALB HTTPS listener. Must be in us-east-1."
  default     = ""
}

# ── RDS ───────────────────────────────────────────────────────────────────────

variable "rds_instance_class" {
  type    = string
  default = "db.t3.small"
}

variable "rds_allocated_storage_gb" {
  type    = number
  default = 20
}

# ── ElastiCache ───────────────────────────────────────────────────────────────

variable "redis_node_type" {
  type    = string
  default = "cache.t3.micro"
}

# ── ECS ───────────────────────────────────────────────────────────────────────

variable "api_cpu" {
  type    = number
  default = 512   # 0.5 vCPU
}

variable "api_memory" {
  type    = number
  default = 1024  # 1 GB
}

variable "worker_parse_cpu" {
  type    = number
  default = 2048  # 2 vCPU — pymavlink 6GB BIN parsing is CPU-bound (~2min/file)
}

variable "worker_parse_memory" {
  type    = number
  default = 4096  # 4 GB — large BIN files decompress to ~4× in memory
}

variable "worker_investigate_cpu" {
  type    = number
  default = 1024  # 1 vCPU — LLM pipeline (single-threaded)
}

variable "worker_investigate_memory" {
  type    = number
  default = 2048
}

variable "qdrant_cpu" {
  type    = number
  default = 512
}

variable "qdrant_memory" {
  type    = number
  default = 1024
}

# ── Secrets (initial values; rotate via Secrets Manager console) ──────────────

variable "db_password" {
  type        = string
  sensitive   = true
  description = "RDS master password. Set via TF_VAR_db_password env var."
}

variable "anthropic_api_key" {
  type        = string
  sensitive   = true
  description = "Anthropic API key. Set via TF_VAR_anthropic_api_key env var."
  default     = ""
}

variable "openai_api_key" {
  type        = string
  sensitive   = true
  description = "OpenAI API key. Set via TF_VAR_openai_api_key env var."
  default     = ""
}

variable "api_secret_key" {
  type        = string
  sensitive   = true
  description = "FastAPI JWT signing secret. Set via TF_VAR_api_secret_key env var."
}

# ── Alerts ────────────────────────────────────────────────────────────────────

variable "alert_email" {
  type        = string
  description = "Email address for CloudWatch alarm notifications."
  default     = ""
}
