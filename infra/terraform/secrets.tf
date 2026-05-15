# Secrets Manager — all sensitive config for ECS tasks.
# Values are set via terraform variables (TF_VAR_* env vars in CI/CD).
# Rotation can be configured per secret in the AWS console after deployment.

resource "aws_secretsmanager_secret" "db_password" {
  name        = "${local.name_prefix}/db-password"
  description = "RDS PostgreSQL master password"
  tags        = { Name = "${local.name_prefix}-db-password" }
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id     = aws_secretsmanager_secret.db_password.id
  secret_string = var.db_password
}

resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name        = "${local.name_prefix}/anthropic-api-key"
  description = "Anthropic Claude API key"
  tags        = { Name = "${local.name_prefix}-anthropic-key" }
}

resource "aws_secretsmanager_secret_version" "anthropic_api_key" {
  secret_id     = aws_secretsmanager_secret.anthropic_api_key.id
  secret_string = var.anthropic_api_key
}

resource "aws_secretsmanager_secret" "openai_api_key" {
  name        = "${local.name_prefix}/openai-api-key"
  description = "OpenAI GPT-4o-mini and fallback API key"
  tags        = { Name = "${local.name_prefix}-openai-key" }
}

resource "aws_secretsmanager_secret_version" "openai_api_key" {
  secret_id     = aws_secretsmanager_secret.openai_api_key.id
  secret_string = var.openai_api_key
}

resource "aws_secretsmanager_secret" "api_secret_key" {
  name        = "${local.name_prefix}/api-secret-key"
  description = "FastAPI JWT signing secret"
  tags        = { Name = "${local.name_prefix}-api-secret-key" }
}

resource "aws_secretsmanager_secret_version" "api_secret_key" {
  secret_id     = aws_secretsmanager_secret.api_secret_key.id
  secret_string = var.api_secret_key
}

# ── Convenience locals for ECS secret references ──────────────────────────────

locals {
  # ECS secret definition format: { name = ENV_VAR_NAME, valueFrom = ARN }
  common_secrets = [
    {
      name      = "ANTHROPIC_API_KEY"
      valueFrom = aws_secretsmanager_secret.anthropic_api_key.arn
    },
    {
      name      = "OPENAI_API_KEY"
      valueFrom = aws_secretsmanager_secret.openai_api_key.arn
    },
    {
      name      = "SECRET_KEY"
      valueFrom = aws_secretsmanager_secret.api_secret_key.arn
    },
  ]

  db_secret = {
    name      = "DB_PASSWORD"
    valueFrom = aws_secretsmanager_secret.db_password.arn
  }

  redis_auth_secret = {
    name      = "REDIS_AUTH_TOKEN"
    valueFrom = aws_secretsmanager_secret.redis_auth.arn
  }
}
