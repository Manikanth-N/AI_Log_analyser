output "alb_dns_name" {
  description = "ALB DNS name — point your api.yourdomain.com CNAME here"
  value       = aws_lb.api.dns_name
}

output "cloudfront_domain" {
  description = "CloudFront distribution domain — CNAME your root domain here"
  value       = aws_cloudfront_distribution.frontend.domain_name
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID — needed for cache invalidation in CI/CD"
  value       = aws_cloudfront_distribution.frontend.id
}

output "ecr_api_repository_url" {
  description = "ECR URL for the API image — set as ECR_API_REPO in GitHub secrets"
  value       = aws_ecr_repository.api.repository_url
}

output "ecr_worker_repository_url" {
  description = "ECR URL for the worker image — set as ECR_WORKER_REPO in GitHub secrets"
  value       = aws_ecr_repository.worker.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name — set as ECS_CLUSTER in GitHub secrets"
  value       = aws_ecs_cluster.main.name
}

output "frontend_bucket_name" {
  description = "S3 bucket for frontend assets — set as FRONTEND_BUCKET in GitHub secrets"
  value       = aws_s3_bucket.frontend.id
}

output "rds_endpoint" {
  description = "RDS endpoint (internal DNS)"
  value       = aws_db_instance.main.address
  sensitive   = true
}

output "redis_primary_endpoint" {
  description = "ElastiCache primary endpoint (internal DNS)"
  value       = aws_elasticache_replication_group.redis.primary_endpoint_address
  sensitive   = true
}

output "github_actions_role_arn" {
  description = "IAM role ARN for GitHub Actions OIDC — set as AWS_ROLE_ARN in GitHub secrets"
  value       = aws_iam_role.github_actions.arn
}

output "data_bucket_name" {
  description = "S3 bucket for flight data (raw logs, parquet, reports)"
  value       = aws_s3_bucket.data.id
}
