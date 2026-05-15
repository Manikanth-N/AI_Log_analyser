# Forensic Flight AI — AWS Deployment Runbook

## Architecture

```
User Browser
  → CloudFront [edge TLS, global CDN]
      ├─ S3 [forensic-flight-prod-frontend-*]
      │   React SPA (Vite build, static assets)
      │
      └─ ALB [forensic-flight-prod-alb]  HTTPS :443 → HTTP :8000
            └─ ECS Fargate: API [0.5 vCPU / 1 GB]
                  Private subnet, no public IP
                  ↓
           ┌──────────────────────────────────────────────────┐
           │  ECS Fargate services (private subnet)            │
           │  • Worker-Parse      [1 vCPU / 2 GB, concurrency=2] │
           │  • Worker-Investigate[1 vCPU / 2 GB, concurrency=1] │
           │  • Qdrant            [0.5 vCPU / 1 GB + EFS]     │
           └──────────────────────────────────────────────────┘
                  ↓                     ↓
     ElastiCache Redis (TLS)    RDS PostgreSQL 16
     [cache.t3.micro]           [db.t3.small, single-AZ]
                  ↓
     S3 [forensic-flight-prod-data-*]
     (raw uploads, parquet store, reports)
                  ↓ (outbound via NAT Gateway)
     Anthropic API  /  OpenAI API

     Secrets Manager ← all ECS tasks read at startup
     CloudWatch Logs ← all services stream to
```

**Inference routing:**
- Domain agents (EKF, GPS, Power, Vibration) → `gpt-4o-mini-2024-07-18`
- Critical path (CrashInvestigator, ReportWriter) → `claude-sonnet-4-6`
- Fallback → `gpt-4o-2024-11-20`

---

## Prerequisites

- AWS CLI v2 configured with admin credentials
- Terraform >= 1.7 installed
- Docker installed
- `aws`, `terraform`, `jq` in PATH

---

## Step 0: Bootstrap (one-time, manual)

These resources cannot be created by Terraform because Terraform needs them to store its own state.

```bash
# 1. Create S3 bucket for Terraform state
aws s3 mb s3://forensic-flight-tfstate --region us-east-1

# Enable versioning (allows rollback of state)
aws s3api put-bucket-versioning \
  --bucket forensic-flight-tfstate \
  --versioning-configuration Status=Enabled

# Enable encryption
aws s3api put-bucket-encryption \
  --bucket forensic-flight-tfstate \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# Block public access
aws s3api put-public-access-block \
  --bucket forensic-flight-tfstate \
  --public-access-block-configuration \
  'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'

# 2. Create DynamoDB table for state locking
aws dynamodb create-table \
  --table-name forensic-flight-tfstate-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1
```

---

## Step 1: Configure Terraform variables

```bash
cd infra/terraform

# Copy example and edit
cp terraform.tfvars.example terraform.tfvars
vim terraform.tfvars  # set domain_name if you have one

# Set sensitive variables via environment (NEVER put in tfvars)
export TF_VAR_db_password="$(openssl rand -base64 32 | tr -d '=/+' | head -c 32)"
export TF_VAR_api_secret_key="$(openssl rand -base64 32)"
export TF_VAR_anthropic_api_key="sk-ant-api03-..."   # your key
export TF_VAR_openai_api_key="sk-..."                # your key
export TF_VAR_github_repo="yourorg/forensic-flight"  # for OIDC trust

# Store db_password and api_secret_key somewhere safe (1Password / AWS Secrets Manager)
# You will need them again if you ever recreate the stack.
```

---

## Step 2: Initialize Terraform

```bash
cd infra/terraform
terraform init

# Verify state backend connected
terraform state list  # should return empty (new stack)
```

---

## Step 3: Plan and apply (exact resource creation order)

Terraform handles dependency ordering automatically. Apply is idempotent.

```bash
# Preview all changes
terraform plan -out=tfplan

# Review the plan carefully, then apply
terraform apply tfplan
```

**Resources are created in this effective order:**
1. VPC, subnets, IGW, NAT Gateway, route tables
2. Security groups
3. RDS subnet group → RDS PostgreSQL
4. ElastiCache subnet group → Redis auth secret → ElastiCache
5. S3 buckets (data + frontend)
6. ECR repositories
7. IAM roles (execution + task + GitHub Actions)
8. Secrets Manager secrets (DB password, API keys)
9. EFS file system + mount targets
10. ALB + target group + listeners
11. CloudWatch log groups + alarms + SNS
12. ECS cluster
13. ECS task definitions (Qdrant, API, workers)
14. ECS services (Qdrant first, then API + workers)
15. CloudFront distribution + S3 bucket policy

**Expected apply time: 15–25 minutes** (RDS and ElastiCache dominate)

---

## Step 4: Record outputs

```bash
terraform output

# Record these values in GitHub Actions secrets:
terraform output -raw alb_dns_name              # → ALB_DNS_NAME
terraform output -raw cloudfront_domain         # → (CNAME target)
terraform output -raw cloudfront_distribution_id # → CLOUDFRONT_DISTRIBUTION_ID
terraform output -raw ecr_api_repository_url    # → ECR_API_REPO
terraform output -raw ecr_worker_repository_url # → ECR_WORKER_REPO
terraform output -raw ecs_cluster_name          # → ECS_CLUSTER
terraform output -raw frontend_bucket_name      # → FRONTEND_BUCKET
terraform output -raw github_actions_role_arn   # → AWS_ROLE_ARN
```

---

## Step 5: Set GitHub Actions secrets

Go to **GitHub → Settings → Secrets and variables → Actions** and create:

| Secret name                  | Value source                     |
|------------------------------|----------------------------------|
| `AWS_ROLE_ARN`               | `terraform output github_actions_role_arn` |
| `ECR_API_REPO`               | `terraform output ecr_api_repository_url`  |
| `ECR_WORKER_REPO`            | `terraform output ecr_worker_repository_url` |
| `ECS_CLUSTER`                | `terraform output ecs_cluster_name`        |
| `FRONTEND_BUCKET`            | `terraform output frontend_bucket_name`    |
| `CLOUDFRONT_DISTRIBUTION_ID` | `terraform output cloudfront_distribution_id` |
| `ALB_DNS_NAME`               | `terraform output alb_dns_name`            |
| `VITE_API_URL`               | `https://api.yourdomain.com` or `http://<ALB_DNS_NAME>` |

---

## Step 6: First deploy

```bash
# Push to main to trigger the deploy workflow, OR run manually:
git push origin main

# Monitor the GitHub Actions run at:
# https://github.com/yourorg/forensic-flight/actions
```

**First deploy initialises the database schema.** The API container runs
`create_tables()` at startup — verify this happened:

```bash
# Check API logs
aws logs tail /ecs/forensic-flight-prod/api --follow --since 5m
```

---

## Step 7: DNS configuration (if using a custom domain)

```bash
# Get ALB DNS name
ALB_DNS=$(terraform output -raw alb_dns_name)
CF_DOMAIN=$(terraform output -raw cloudfront_domain)

echo "Create these DNS records:"
echo "  api.yourdomain.com  CNAME  $ALB_DNS"
echo "  yourdomain.com      CNAME  $CF_DOMAIN"
echo "  www.yourdomain.com  CNAME  $CF_DOMAIN"
```

**ACM certificate prerequisite:** Create an ACM cert in us-east-1 (for CloudFront) via
`aws acm request-certificate --domain-name "*.yourdomain.com" ...` then set
`acm_certificate_arn` in `terraform.tfvars` and re-apply.

---

## Step 8: Verify deployment

```bash
ALB=$(terraform output -raw alb_dns_name)

# Health check
curl http://$ALB/health
# Expected: {"status": "ok"}

# Upload a test log (replace with a real .bin or .ulg file path)
curl -X POST http://$ALB/api/v1/flights/upload \
  -H "X-API-Key: <your-api-key>" \
  -F "file=@logs/test_flight.bin"
```

---

## Operational runbook

### View logs

```bash
# API
aws logs tail /ecs/forensic-flight-prod/api --follow

# Parse worker
aws logs tail /ecs/forensic-flight-prod/worker-parse --follow

# Investigation worker
aws logs tail /ecs/forensic-flight-prod/worker-investigate --follow
```

### Debug shell into a running ECS task

```bash
CLUSTER="forensic-flight-prod-cluster"

# Get running API task ARN
TASK=$(aws ecs list-tasks --cluster $CLUSTER \
  --service-name forensic-flight-prod-api \
  --query 'taskArns[0]' --output text)

# Open shell
aws ecs execute-command \
  --cluster $CLUSTER \
  --task $TASK \
  --container api \
  --interactive \
  --command "/bin/bash"
```

### Scale workers

```bash
# Scale investigation workers to 2 (process 2 investigations in parallel)
aws ecs update-service \
  --cluster forensic-flight-prod-cluster \
  --service forensic-flight-prod-worker-investigate \
  --desired-count 2

# Scale back down
aws ecs update-service \
  --cluster forensic-flight-prod-cluster \
  --service forensic-flight-prod-worker-investigate \
  --desired-count 1
```

### Force a redeployment (without code change)

```bash
aws ecs update-service \
  --cluster forensic-flight-prod-cluster \
  --service forensic-flight-prod-api \
  --force-new-deployment
```

### Rollback a bad deploy

```bash
# List recent task definition revisions
aws ecs list-task-definitions \
  --family-prefix forensic-flight-prod-api \
  --sort DESC --query 'taskDefinitionArns[:5]'

# Roll back to previous revision
aws ecs update-service \
  --cluster forensic-flight-prod-cluster \
  --service forensic-flight-prod-api \
  --task-definition forensic-flight-prod-api:PREVIOUS_REVISION
```

### Rotate API keys

```bash
# Update OpenAI key (example)
aws secretsmanager put-secret-value \
  --secret-id forensic-flight-prod/openai-api-key \
  --secret-string "sk-new-key-here"

# Force service restart to pick up new secret
aws ecs update-service \
  --cluster forensic-flight-prod-cluster \
  --service forensic-flight-prod-api \
  --force-new-deployment

aws ecs update-service \
  --cluster forensic-flight-prod-cluster \
  --service forensic-flight-prod-worker-investigate \
  --force-new-deployment
```

### RDS database backup and restore

```bash
# Manual snapshot before a risky migration
aws rds create-db-snapshot \
  --db-instance-identifier forensic-flight-prod-postgres \
  --db-snapshot-identifier forensic-flight-pre-migration-$(date +%Y%m%d)

# List available snapshots
aws rds describe-db-snapshots \
  --db-instance-identifier forensic-flight-prod-postgres \
  --query 'DBSnapshots[].{ID:DBSnapshotIdentifier,Created:SnapshotCreateTime}' \
  --output table
```

---

## Cost summary (us-east-1, MVP sizing)

| Service                  | Size              | $/month (est.) |
|--------------------------|-------------------|----------------|
| ECS Fargate — API        | 0.5 vCPU / 1 GB  | ~$14           |
| ECS Fargate — Worker×2   | 1 vCPU / 2 GB ×2 | ~$56           |
| ECS Fargate — Qdrant     | 0.5 vCPU / 1 GB  | ~$14           |
| ElastiCache              | cache.t3.micro    | ~$13           |
| RDS PostgreSQL           | db.t3.small       | ~$30           |
| ALB                      | ~5k req/day       | ~$18           |
| NAT Gateway              | 1 gateway         | ~$32           |
| S3 (data + frontend)     | <50 GB            | ~$5            |
| Secrets Manager          | 5 secrets         | ~$3            |
| CloudFront               | low traffic       | ~$2            |
| EFS (Qdrant storage)     | <2 GB             | ~$1            |
| CloudWatch               | logs + alarms     | ~$5            |
| ECR                      | <5 GB             | ~$1            |
| **Total**                |                   | **~$195/month**|

**Cost reduction options:**
- Use Fargate Spot for worker-parse (70% cheaper, acceptable for parse tasks): saves ~$20/mo
- Downgrade RDS to db.t3.micro if load is light: saves ~$15/mo
- Minimum: ~$160/month if using Spot for workers + micro RDS

**LLM API costs (separate from infra):**
- GPT-4o-mini domain calls: ~$0.15/M input + $0.60/M output
- Claude Sonnet critical calls: ~$3/M input + $15/M output
- Estimated per investigation: ~$0.10–0.40 depending on log size
- 100 investigations/month: ~$10–40

---

## Upgrade path

| Trigger                       | Action                                              |
|-------------------------------|-----------------------------------------------------|
| >5 concurrent investigations  | Scale worker-investigate desired_count to 3-5       |
| RDS CPU consistently >70%     | Upgrade to db.t3.medium                             |
| Redis memory >70%             | Upgrade to cache.t3.small                           |
| NAT Gateway data cost high    | Add VPC endpoints for S3 + Secrets Manager          |
| RDS single-AZ risk concern    | Enable multi_az=true in terraform (~2× cost)        |
| >2,000 investigations/day     | Evaluate vLLM self-hosted at break-even             |
