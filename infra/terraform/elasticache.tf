resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.name_prefix}-redis-subnet-group"
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = "${local.name_prefix}-redis-subnet-group" }
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id = "${local.name_prefix}-redis"
  description          = "Forensic Flight Redis - Celery broker + pubsub"

  node_type            = var.redis_node_type
  port                 = 6379
  parameter_group_name = "default.redis7"
  engine_version       = "7.1"

  # Single node for MVP (num_cache_clusters=1).
  # Set num_cache_clusters=2 and automatic_failover_enabled=true for HA.
  num_cache_clusters         = 1
  automatic_failover_enabled = false
  multi_az_enabled           = false

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = aws_secretsmanager_secret_version.redis_auth.secret_string

  # In-transit encryption requires AUTH token
  transit_encryption_mode = "required"

  snapshot_retention_limit = 1
  snapshot_window          = "02:00-03:00"

  tags = { Name = "${local.name_prefix}-redis" }

  depends_on = [aws_secretsmanager_secret_version.redis_auth]
}

# Redis AUTH token — generated once, stored in Secrets Manager
resource "random_password" "redis_auth" {
  length  = 32
  special = false # Redis auth tokens: alphanumeric only
}

resource "aws_secretsmanager_secret" "redis_auth" {
  name        = "${local.name_prefix}/redis-auth-token"
  description = "ElastiCache Redis AUTH token"
  tags        = { Name = "${local.name_prefix}-redis-auth" }
}

resource "aws_secretsmanager_secret_version" "redis_auth" {
  secret_id     = aws_secretsmanager_secret.redis_auth.id
  secret_string = random_password.redis_auth.result
}
