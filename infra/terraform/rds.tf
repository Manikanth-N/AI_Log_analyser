resource "aws_db_subnet_group" "main" {
  name       = "${local.name_prefix}-db-subnet-group"
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = "${local.name_prefix}-db-subnet-group" }
}

resource "aws_db_parameter_group" "postgres16" {
  name   = "${local.name_prefix}-pg16"
  family = "postgres16"

  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }

  tags = { Name = "${local.name_prefix}-pg16-params" }
}

resource "aws_db_instance" "main" {
  identifier = "${local.name_prefix}-postgres"

  engine               = "postgres"
  engine_version       = "16.2"
  instance_class       = var.rds_instance_class
  allocated_storage    = var.rds_allocated_storage_gb
  max_allocated_storage = var.rds_allocated_storage_gb * 3  # auto-scale up to 3×

  db_name  = "forensic_flight"
  username = "forensic"
  password = var.db_password

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.postgres16.name

  multi_az            = false  # single-AZ for MVP; enable for HA production
  publicly_accessible = false
  storage_encrypted   = true
  deletion_protection = true

  backup_retention_period = 7
  backup_window           = "03:00-04:00"  # UTC
  maintenance_window      = "sun:04:00-sun:05:00"

  performance_insights_enabled = true

  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name_prefix}-final-${local.suffix}"

  tags = { Name = "${local.name_prefix}-postgres" }
}
