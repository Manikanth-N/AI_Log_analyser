#!/usr/bin/env bash
# deploy/bootstrap.sh — Phase 6 Step 1: AWS Bootstrap
# Run ONCE before the first terraform apply.
# Safe to re-run: all create commands are idempotent.
#
# Prerequisites:
#   - An AWS account with AdministratorAccess (or scoped IAM)
#   - Your AWS Access Key ID + Secret (for bootstrap only; replaced by OIDC after)
#   - A GitHub repo to wire OIDC to (e.g. myorg/AI_Log_analyser)
#   - Docker installed (already confirmed)
#
# Usage:
#   export AWS_ACCOUNT_ID=123456789012
#   export GITHUB_REPO=myorg/AI_Log_analyser      # no leading slash, exact case
#   bash deploy/bootstrap.sh

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────

AWS_REGION="${AWS_REGION:-us-east-1}"
STATE_BUCKET="forensic-flight-tfstate"
LOCK_TABLE="forensic-flight-tfstate-lock"
TERRAFORM_VERSION="1.8.5"

# GitHub's published OIDC thumbprint for token.actions.githubusercontent.com
# Source: https://github.blog/changelog/2023-06-27-github-actions-update-on-oidc-integration-with-aws/
# AWS IAM also maintains its own CA trust list for this provider (thumbprint
# is no longer the enforcement mechanism), but use the correct published value.
GITHUB_OIDC_THUMBPRINT="6938fd4d98bab03faadb97b34396831e3780aea1"

# These must be set by the caller
: "${AWS_ACCOUNT_ID:?Set AWS_ACCOUNT_ID=<your 12-digit account ID>}"
: "${GITHUB_REPO:?Set GITHUB_REPO=owner/repo (e.g. myorg/AI_Log_analyser)}"

# ── Helpers ───────────────────────────────────────────────────────────────────

ok()   { printf "\033[0;32m[OK]\033[0m  %s\n" "$*"; }
info() { printf "\033[0;34m[--]\033[0m  %s\n" "$*"; }
fail() { printf "\033[0;31m[!!]\033[0m  %s\n" "$*"; exit 1; }

# ── Input validation ──────────────────────────────────────────────────────────

validate_inputs() {
  # AWS account ID must be exactly 12 digits
  if ! [[ "$AWS_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
    fail "AWS_ACCOUNT_ID must be exactly 12 digits. Got: '$AWS_ACCOUNT_ID'"
  fi
  ok "AWS_ACCOUNT_ID format valid."

  # GITHUB_REPO must be owner/repo — alphanumeric, hyphens, underscores, dots only
  # Prevents JSON injection when interpolated into the trust policy heredoc
  if ! [[ "$GITHUB_REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    fail "GITHUB_REPO must be 'owner/repo' with [A-Za-z0-9_.-] characters only. Got: '$GITHUB_REPO'"
  fi
  ok "GITHUB_REPO format valid."
}

# ── Step 1: AWS CLI ───────────────────────────────────────────────────────────

install_aws_cli() {
  if command -v aws &>/dev/null; then
    ok "AWS CLI already installed: $(aws --version)"
    return
  fi

  info "Installing AWS CLI v2 ..."
  local arch
  arch=$(uname -m)
  case "$arch" in
    x86_64)  arch=x86_64  ;;
    aarch64) arch=aarch64 ;;
    arm64)   arch=aarch64 ;;
    *)       fail "Unsupported architecture: $arch" ;;
  esac

  local tmpdir
  tmpdir=$(mktemp -d)
  # -f: fail on HTTP error  -s: silent  -S: show errors  -L: follow redirects
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${arch}.zip" \
    -o "$tmpdir/awscliv2.zip"

  unzip -q "$tmpdir/awscliv2.zip" -d "$tmpdir"
  sudo "$tmpdir/aws/install" --update
  rm -rf "$tmpdir"

  ok "AWS CLI installed: $(aws --version)"
}

# ── Step 2: Configure AWS credentials ─────────────────────────────────────────

configure_aws() {
  if aws sts get-caller-identity &>/dev/null; then
    local identity
    identity=$(aws sts get-caller-identity --query 'Arn' --output text)
    ok "AWS credentials active: $identity"
    return
  fi

  info "No active AWS credentials found."
  echo ""
  echo "  You need IAM credentials with AdministratorAccess (for bootstrap only)."
  echo "  These are used ONCE to create the OIDC trust and state bucket."
  echo "  GitHub Actions will use OIDC — no static keys stored after this."
  echo ""
  echo "  To get credentials:"
  echo "    AWS Console → IAM → Users → <your user> → Security credentials → Create access key"
  echo ""
  aws configure set region "$AWS_REGION"
  aws configure

  aws sts get-caller-identity &>/dev/null \
    || fail "Credential test failed. Check key ID and secret."
  ok "AWS credentials configured."
}

# ── Step 3: Terraform ─────────────────────────────────────────────────────────

install_terraform() {
  if command -v terraform &>/dev/null; then
    local ver
    ver=$(terraform version -json | python3 -c "import sys,json; print(json.load(sys.stdin)['terraform_version'])")
    ok "Terraform already installed: v${ver}"
    return
  fi

  info "Installing Terraform v${TERRAFORM_VERSION} ..."
  local arch
  arch=$(uname -m)
  case "$arch" in
    x86_64)  arch=amd64 ;;
    aarch64) arch=arm64 ;;
    arm64)   arch=arm64 ;;
    *)       fail "Unsupported architecture: $arch" ;;
  esac

  local base_url="https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}"
  local zipfile="terraform_${TERRAFORM_VERSION}_linux_${arch}.zip"

  local tmpdir
  tmpdir=$(mktemp -d)

  # Download binary zip and HashiCorp's SHA256SUMS manifest.
  # Save zip under its canonical name so sha256sum -c can resolve it by filename.
  curl -fsSL "${base_url}/${zipfile}" -o "$tmpdir/${zipfile}"
  curl -fsSL "${base_url}/terraform_${TERRAFORM_VERSION}_SHA256SUMS" \
    -o "$tmpdir/SHA256SUMS"

  # Extract only our target line from the manifest so sha256sum -c checks exactly
  # one file. grep fails (exit 1) if the pattern is absent — caught by set -e.
  info "Verifying Terraform SHA256 ..."
  grep "^[0-9a-f]\{64\}  ${zipfile}$" "$tmpdir/SHA256SUMS" \
    > "$tmpdir/check.sha256" \
    || fail "Checksum entry for '${zipfile}' not found in SHA256SUMS — incomplete download?"

  # sha256sum -c reads "hash  filename" pairs and verifies each file in CWD.
  ( cd "$tmpdir" && sha256sum -c check.sha256 ) \
    || fail "Terraform SHA256 verification FAILED — binary may be corrupted or tampered."
  ok "Terraform SHA256 verified."

  unzip -q "$tmpdir/${zipfile}" -d "$tmpdir"
  sudo mv "$tmpdir/terraform" /usr/local/bin/terraform
  sudo chmod +x /usr/local/bin/terraform
  rm -rf "$tmpdir"

  ok "Terraform installed: $(terraform version -no-color | head -1)"
}

# ── Step 4: Terraform state bucket + DynamoDB lock table ──────────────────────

create_state_backend() {
  info "Creating Terraform state backend ..."

  # S3 state bucket
  if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
    ok "State bucket already exists: s3://$STATE_BUCKET"
  else
    # Note: us-east-1 must NOT use --create-bucket-configuration (AWS restriction)
    aws s3api create-bucket \
      --bucket "$STATE_BUCKET" \
      --region "$AWS_REGION"

    aws s3api put-bucket-versioning \
      --bucket "$STATE_BUCKET" \
      --versioning-configuration Status=Enabled

    aws s3api put-bucket-encryption \
      --bucket "$STATE_BUCKET" \
      --server-side-encryption-configuration '{
        "Rules": [{
          "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
          "BucketKeyEnabled": true
        }]
      }'

    aws s3api put-public-access-block \
      --bucket "$STATE_BUCKET" \
      --public-access-block-configuration \
        BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

    # Deny all non-TLS requests. State contains DB passwords and API keys.
    aws s3api put-bucket-policy \
      --bucket "$STATE_BUCKET" \
      --policy "$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "DenyNonSSL",
    "Effect": "Deny",
    "Principal": "*",
    "Action": "s3:*",
    "Resource": [
      "arn:aws:s3:::${STATE_BUCKET}",
      "arn:aws:s3:::${STATE_BUCKET}/*"
    ],
    "Condition": {
      "Bool": {"aws:SecureTransport": "false"}
    }
  }]
}
EOF
)"

    ok "State bucket created: s3://$STATE_BUCKET (versioned, encrypted, SSL-only)"
  fi

  # DynamoDB lock table
  if aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "$AWS_REGION" &>/dev/null; then
    ok "Lock table already exists: $LOCK_TABLE"
  else
    aws dynamodb create-table \
      --table-name "$LOCK_TABLE" \
      --attribute-definitions AttributeName=LockID,AttributeType=S \
      --key-schema AttributeName=LockID,KeyType=HASH \
      --billing-mode PAY_PER_REQUEST \
      --deletion-protection-enabled \
      --region "$AWS_REGION"

    aws dynamodb wait table-exists \
      --table-name "$LOCK_TABLE" \
      --region "$AWS_REGION"

    ok "Lock table created: $LOCK_TABLE (deletion-protected)"
  fi
}

# ── Step 5: GitHub OIDC identity provider ─────────────────────────────────────

setup_github_oidc() {
  local oidc_url="https://token.actions.githubusercontent.com"
  local oidc_arn="arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"

  info "Setting up GitHub OIDC identity provider ..."

  if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$oidc_arn" &>/dev/null; then
    ok "GitHub OIDC provider already registered."
  else
    # Use GitHub's published static thumbprint.
    # Dynamic openssl extraction would fetch the leaf cert fingerprint, not the
    # intermediate CA thumbprint that AWS actually requires — and silently fail
    # if the TLS negotiation returns nothing. Static value is safer.
    aws iam create-open-id-connect-provider \
      --url "$oidc_url" \
      --client-id-list "sts.amazonaws.com" \
      --thumbprint-list "$GITHUB_OIDC_THUMBPRINT"

    ok "GitHub OIDC provider created (thumbprint: ${GITHUB_OIDC_THUMBPRINT})."
  fi

  # GitHub Actions deployment role
  local role_name="forensic-flight-github-actions"
  local role_arn="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${role_name}"

  if aws iam get-role --role-name "$role_name" &>/dev/null; then
    ok "GitHub Actions IAM role already exists: $role_name"
  else
    info "Creating GitHub Actions IAM role ..."

    # Trust policy:
    # - StringLike on :sub restricts to this repo only (all branches/tags)
    # - StringEquals on :aud prevents token reuse from other OIDC consumers
    # GITHUB_REPO validated above to [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+ — safe to interpolate
    aws iam create-role \
      --role-name "$role_name" \
      --assume-role-policy-document "$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:${GITHUB_REPO}:*"
      },
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      }
    }
  }]
}
EOF
)"

    # Permission policy — bootstrap-stage scope notes:
    #
    #   ECRAuth    Resource="*" — ecr:GetAuthorizationToken cannot be scoped by AWS design.
    #
    #   ECRPush    Resource="*" — ECR repository ARNs do not exist until `terraform apply`.
    #   TODO(post-deploy): replace "*" with the two ECR repo ARNs from terraform output.
    #
    #   ECSUpdate  Resource="*" — ECS cluster/service ARNs do not exist until `terraform apply`.
    #              ecs:RegisterTaskDefinition also cannot be scoped to specific task def ARNs
    #              (new revision is the output, not the input).
    #   TODO(post-deploy): scope ecs:UpdateService to the cluster ARN from terraform output.
    #
    #   CloudFrontInvalidate Resource="*" — CloudFront distribution ARN not yet known.
    #   TODO(post-deploy): replace "*" with the distribution ARN from terraform output.
    #
    # Privilege escalation analysis:
    #   - No iam:CreateRole / iam:AttachRolePolicy / iam:PutRolePolicy — cannot self-escalate.
    #   - iam:PassRole is scoped to exactly the two ECS roles — cannot pass to arbitrary roles.
    #   - ecs:RegisterTaskDefinition + iam:PassRole(scoped) is the irreducible minimum for ECS deploy.
    aws iam put-role-policy \
      --role-name "$role_name" \
      --policy-name "forensic-flight-deploy-policy" \
      --policy-document "$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ECRAuth",
      "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken"],
      "Resource": "*"
    },
    {
      "Sid": "ECRPush",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:CompleteLayerUpload",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
        "ecr:BatchGetImage",
        "ecr:DescribeRepositories"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ECSUpdate",
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeServices",
        "ecs:DescribeTaskDefinition",
        "ecs:RegisterTaskDefinition",
        "ecs:UpdateService",
        "ecs:DescribeTasks",
        "ecs:ListTasks"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3FrontendDeploy",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:ListBucket",
        "s3:DeleteObject"
      ],
      "Resource": [
        "arn:aws:s3:::forensic-flight-prod-frontend-*",
        "arn:aws:s3:::forensic-flight-prod-frontend-*/*"
      ]
    },
    {
      "Sid": "CloudFrontInvalidate",
      "Effect": "Allow",
      "Action": ["cloudfront:CreateInvalidation"],
      "Resource": "*"
    },
    {
      "Sid": "PassRoleToECS",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::${AWS_ACCOUNT_ID}:role/forensic-flight-prod-ecs-execution-role",
        "arn:aws:iam::${AWS_ACCOUNT_ID}:role/forensic-flight-prod-ecs-task-role"
      ]
    },
    {
      "Sid": "TerraformState",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::${STATE_BUCKET}",
        "arn:aws:s3:::${STATE_BUCKET}/*"
      ]
    },
    {
      "Sid": "TerraformLock",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:DeleteItem"
      ],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${LOCK_TABLE}"
    }
  ]
}
EOF
)"

    ok "GitHub Actions IAM role created: $role_arn"
  fi

  echo ""
  echo "  ┌─────────────────────────────────────────────────────────────────────┐"
  echo "  │  Add to GitHub: Settings → Secrets and variables → Actions          │"
  echo "  │                                                                     │"
  printf "  │  AWS_ROLE_ARN = %-54s│\n" "$role_arn"
  echo "  └─────────────────────────────────────────────────────────────────────┘"
  echo ""
}

# ── Step 6: Validate all components ───────────────────────────────────────────

validate_bootstrap() {
  info "Running post-bootstrap validation ..."

  local errors=0

  # AWS identity
  if aws sts get-caller-identity --query 'Arn' --output text 2>/dev/null | grep -q "arn:aws"; then
    ok "AWS identity confirmed."
  else
    printf "\033[0;31m[!!]\033[0m  AWS identity check failed.\n"; errors=$((errors+1))
  fi

  # State bucket
  if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
    ok "State bucket accessible: s3://$STATE_BUCKET"
  else
    printf "\033[0;31m[!!]\033[0m  State bucket not accessible.\n"; errors=$((errors+1))
  fi

  # DynamoDB lock table
  local table_status
  table_status=$(aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "$AWS_REGION" \
    --query 'Table.TableStatus' --output text 2>/dev/null || echo "MISSING")
  if [[ "$table_status" == "ACTIVE" ]]; then
    ok "DynamoDB lock table ACTIVE."
  else
    printf "\033[0;31m[!!]\033[0m  DynamoDB lock table status: $table_status\n"; errors=$((errors+1))
  fi

  # OIDC provider
  local oidc_arn="arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
  if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$oidc_arn" &>/dev/null; then
    ok "GitHub OIDC provider registered."
  else
    printf "\033[0;31m[!!]\033[0m  GitHub OIDC provider not found.\n"; errors=$((errors+1))
  fi

  # GitHub Actions IAM role
  if aws iam get-role --role-name "forensic-flight-github-actions" &>/dev/null; then
    ok "GitHub Actions IAM role exists."
  else
    printf "\033[0;31m[!!]\033[0m  GitHub Actions IAM role not found.\n"; errors=$((errors+1))
  fi

  # Terraform
  if terraform version &>/dev/null; then
    ok "Terraform on PATH."
  else
    printf "\033[0;31m[!!]\033[0m  Terraform not on PATH.\n"; errors=$((errors+1))
  fi

  # Docker
  if docker info &>/dev/null; then
    ok "Docker daemon running."
  else
    printf "\033[0;31m[!!]\033[0m  Docker daemon not running.\n"; errors=$((errors+1))
  fi

  echo ""
  if [[ $errors -gt 0 ]]; then
    fail "$errors validation check(s) failed. Fix above errors before proceeding."
  fi

  echo "  ╔═════════════════════════════════════════════════════════════╗"
  echo "  ║  Bootstrap complete. All 7 checks passed.                   ║"
  echo "  ║                                                             ║"
  echo "  ║  Next: Step 2 — terraform apply                             ║"
  echo "  ║                                                             ║"
  echo "  ║  cd infra/terraform                                         ║"
  echo "  ║  terraform init                                             ║"
  echo "  ║  terraform plan -out=tfplan                                 ║"
  echo "  ║  terraform apply tfplan                                     ║"
  echo "  ╚═════════════════════════════════════════════════════════════╝"
  echo ""
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
  echo ""
  echo "  Forensic Flight AI — Phase 6 Bootstrap"
  echo "  Account : $AWS_ACCOUNT_ID"
  echo "  Region  : $AWS_REGION"
  echo "  Repo    : $GITHUB_REPO"
  echo ""

  validate_inputs
  install_aws_cli
  configure_aws
  install_terraform
  create_state_backend
  setup_github_oidc
  validate_bootstrap
}

main "$@"
