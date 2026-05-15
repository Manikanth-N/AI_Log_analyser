# ── ECS Cluster ───────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = { Name = "${local.name_prefix}-cluster" }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }
}

# ── CloudWatch Log Groups ─────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name_prefix}/api"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "worker_parse" {
  name              = "/ecs/${local.name_prefix}/worker-parse"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "worker_investigate" {
  name              = "/ecs/${local.name_prefix}/worker-investigate"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "qdrant" {
  name              = "/ecs/${local.name_prefix}/qdrant"
  retention_in_days = 14
}

# ── EFS for Qdrant persistent storage ────────────────────────────────────────

resource "aws_efs_file_system" "qdrant" {
  creation_token   = "${local.name_prefix}-qdrant"
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  tags = { Name = "${local.name_prefix}-qdrant-efs" }
}

resource "aws_efs_mount_target" "qdrant" {
  count           = length(var.availability_zones)
  file_system_id  = aws_efs_file_system.qdrant.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}

# ── EFS for shared app storage (raw uploads + Parquet) ───────────────────────
# API and workers share a single EFS so uploaded files are visible to parse workers.

resource "aws_efs_file_system" "app_storage" {
  creation_token   = "${local.name_prefix}-app-storage"
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  tags = { Name = "${local.name_prefix}-app-efs" }
}

resource "aws_efs_mount_target" "app_storage" {
  count           = length(var.availability_zones)
  file_system_id  = aws_efs_file_system.app_storage.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.app_efs.id]
}

resource "aws_efs_access_point" "app_storage" {
  file_system_id = aws_efs_file_system.app_storage.id

  root_directory {
    path = "/storage"
    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "0777"
    }
  }

  tags = { Name = "${local.name_prefix}-app-storage-ap" }
}

# ── Common environment config (shared by API + workers) ───────────────────────

locals {
  redis_url = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0?ssl_cert_reqs=none"

  common_environment = [
    { name = "STORAGE_ROOT", value = "/tmp/storage" },
    { name = "INFERENCE_MODE", value = "api" },
    { name = "DOMAIN_PROVIDER", value = "openai" },
    { name = "DOMAIN_MODEL", value = "gpt-4o-mini-2024-07-18" },
    { name = "CRITICAL_PROVIDER", value = "anthropic" },
    { name = "CRITICAL_MODEL", value = "claude-sonnet-4-6" },
    { name = "FALLBACK_PROVIDER", value = "openai" },
    { name = "FALLBACK_MODEL", value = "gpt-4o-2024-11-20" },
    { name = "EMBEDDING_PROVIDER", value = "openai" },
    { name = "OPENAI_EMBEDDING_MODEL", value = "text-embedding-3-small" },
    { name = "INFERENCE_REQUEST_TIMEOUT_SECONDS", value = "120" },
    { name = "QDRANT_URL", value = "http://qdrant:6333" },
  ]

}

# ── Qdrant Task Definition ────────────────────────────────────────────────────

resource "aws_ecs_task_definition" "qdrant" {
  family                   = "${local.name_prefix}-qdrant"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.qdrant_cpu
  memory                   = var.qdrant_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  volume {
    name = "qdrant-storage"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.qdrant.id
      root_directory     = "/"
      transit_encryption = "ENABLED"
    }
  }

  container_definitions = jsonencode([{
    name      = "qdrant"
    image     = "qdrant/qdrant:v1.7.4"
    essential = true

    portMappings = [{
      name          = "grpc"
      containerPort = 6333
      protocol      = "tcp"
    }]

    mountPoints = [{
      sourceVolume  = "qdrant-storage"
      containerPath = "/qdrant/storage"
      readOnly      = false
    }]

    environment = [
      { name = "QDRANT__SERVICE__GRPC_PORT", value = "6334" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.qdrant.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "qdrant"
      }
    }

  }])
}

resource "aws_ecs_service" "qdrant" {
  name            = "${local.name_prefix}-qdrant"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.qdrant.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.qdrant.id]
    assign_public_ip = false
  }

  # Service Connect: Qdrant is reachable as qdrant.forensic-flight.internal:6333
  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.main.arn

    service {
      port_name      = "grpc"
      discovery_name = "qdrant"
      client_alias {
        port     = 6333
        dns_name = "qdrant"
      }
    }
  }

  deployment_minimum_healthy_percent = 0 # allow full replacement (single task)
  deployment_maximum_percent         = 100

  depends_on = [aws_efs_mount_target.qdrant]

  tags = { Name = "${local.name_prefix}-qdrant" }

  lifecycle {
    ignore_changes = [task_definition]
  }
}

# ── API Task Definition ───────────────────────────────────────────────────────

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  volume {
    name = "app-storage"
    efs_volume_configuration {
      file_system_id          = aws_efs_file_system.app_storage.id
      transit_encryption      = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.app_storage.id
        iam             = "DISABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name      = "api"
    image     = local.api_image
    essential = true

    portMappings = [{
      name          = "http"
      containerPort = 8000
      protocol      = "tcp"
    }]

    mountPoints = [{
      sourceVolume  = "app-storage"
      containerPath = "/tmp/storage"
      readOnly      = false
    }]

    environment = concat(local.common_environment, [
      { name = "DATABASE_URL", value = "postgresql+asyncpg://forensic:${var.db_password}@${aws_db_instance.main.address}:5432/forensic_flight" },
      { name = "DATABASE_URL_SYNC", value = "postgresql+psycopg2://forensic:${var.db_password}@${aws_db_instance.main.address}:5432/forensic_flight" },
      { name = "REDIS_URL", value = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0?ssl_cert_reqs=none" },
      { name = "REDIS_RESULT_URL", value = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/1?ssl_cert_reqs=none" },
      { name = "REDIS_PUBSUB_URL", value = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/2?ssl_cert_reqs=none" },
      { name = "CORS_ORIGINS", value = "[\"https://${var.domain_name}\", \"https://${aws_cloudfront_distribution.frontend.domain_name}\"]" },
    ])

    secrets = local.common_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.api.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "api"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
      interval    = 30
      timeout     = 10
      retries     = 3
      startPeriod = 30
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "${local.name_prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  enable_execute_command = true # allows `aws ecs execute-command` debug shell

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.main.arn
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [
    aws_lb_listener.http,        # redirect 80→443 (when ACM cert provided)
    aws_lb_listener.http_direct, # forward 80→API  (when no cert)
    aws_ecs_service.qdrant,
    aws_efs_mount_target.app_storage,
  ]

  tags = { Name = "${local.name_prefix}-api" }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

# ── Worker — Parse Task Definition ────────────────────────────────────────────

resource "aws_ecs_task_definition" "worker_parse" {
  family                   = "${local.name_prefix}-worker-parse"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_parse_cpu
  memory                   = var.worker_parse_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  volume {
    name = "app-storage"
    efs_volume_configuration {
      file_system_id          = aws_efs_file_system.app_storage.id
      transit_encryption      = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.app_storage.id
        iam             = "DISABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name      = "worker-parse"
    image     = local.worker_image
    essential = true

    command = [
      "celery", "-A", "api.workers.celery_app", "worker",
      "--concurrency=2", "-Q", "parse", "--loglevel=info",
      "--without-gossip", "--without-mingle",
    ]

    mountPoints = [{
      sourceVolume  = "app-storage"
      containerPath = "/tmp/storage"
      readOnly      = false
    }]

    environment = concat(local.common_environment, [
      { name = "DATABASE_URL", value = "postgresql+asyncpg://forensic:${var.db_password}@${aws_db_instance.main.address}:5432/forensic_flight" },
      { name = "DATABASE_URL_SYNC", value = "postgresql+psycopg2://forensic:${var.db_password}@${aws_db_instance.main.address}:5432/forensic_flight" },
      { name = "REDIS_URL", value = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0?ssl_cert_reqs=none" },
      { name = "REDIS_RESULT_URL", value = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/1?ssl_cert_reqs=none" },
      { name = "REDIS_PUBSUB_URL", value = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/2?ssl_cert_reqs=none" },
      { name = "CELERY_BROKER_URL", value = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0?ssl_cert_reqs=CERT_NONE" },
      { name = "CELERY_RESULT_URL", value = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/1?ssl_cert_reqs=CERT_NONE" },
    ])

    secrets = local.common_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker_parse.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "worker-parse"
      }
    }
  }])
}

resource "aws_ecs_service" "worker_parse" {
  name            = "${local.name_prefix}-worker-parse"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker_parse.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  enable_execute_command = true

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = false
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.main.arn
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_efs_mount_target.app_storage]

  tags = { Name = "${local.name_prefix}-worker-parse" }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

# ── Worker — Investigate Task Definition ─────────────────────────────────────

resource "aws_ecs_task_definition" "worker_investigate" {
  family                   = "${local.name_prefix}-worker-investigate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_investigate_cpu
  memory                   = var.worker_investigate_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  volume {
    name = "app-storage"
    efs_volume_configuration {
      file_system_id          = aws_efs_file_system.app_storage.id
      transit_encryption      = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.app_storage.id
        iam             = "DISABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name      = "worker-investigate"
    image     = local.worker_image
    essential = true

    command = [
      "celery", "-A", "api.workers.celery_app", "worker",
      "--concurrency=1", "-Q", "investigate", "--loglevel=info",
      "--without-gossip", "--without-mingle",
    ]

    mountPoints = [{
      sourceVolume  = "app-storage"
      containerPath = "/tmp/storage"
      readOnly      = false
    }]

    environment = concat(local.common_environment, [
      { name = "DATABASE_URL", value = "postgresql+asyncpg://forensic:${var.db_password}@${aws_db_instance.main.address}:5432/forensic_flight" },
      { name = "DATABASE_URL_SYNC", value = "postgresql+psycopg2://forensic:${var.db_password}@${aws_db_instance.main.address}:5432/forensic_flight" },
      { name = "REDIS_URL", value = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0?ssl_cert_reqs=none" },
      { name = "REDIS_RESULT_URL", value = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/1?ssl_cert_reqs=none" },
      { name = "REDIS_PUBSUB_URL", value = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/2?ssl_cert_reqs=none" },
      { name = "CELERY_BROKER_URL", value = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0?ssl_cert_reqs=CERT_NONE" },
      { name = "CELERY_RESULT_URL", value = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/1?ssl_cert_reqs=CERT_NONE" },
    ])

    secrets = local.common_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker_investigate.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "worker-investigate"
      }
    }
  }])
}

resource "aws_ecs_service" "worker_investigate" {
  name            = "${local.name_prefix}-worker-investigate"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker_investigate.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  enable_execute_command = true

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = false
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.main.arn
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_efs_mount_target.app_storage]

  tags = { Name = "${local.name_prefix}-worker-investigate" }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

# ── Service Discovery (ECS Service Connect namespace) ─────────────────────────

resource "aws_service_discovery_http_namespace" "main" {
  name        = "forensic-flight.internal"
  description = "Internal service mesh for Forensic Flight ECS services"
  tags        = { Name = "${local.name_prefix}-sd-namespace" }
}
