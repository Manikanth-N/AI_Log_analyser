#!/bin/bash
# Bootstrap script — run ONCE before first terraform init/apply.
# Creates and hardens the Terraform state bucket and populates secrets.
# Requires: gcloud authenticated with project owner or storage admin rights.
set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID environment variable}"
: "${REGION:=us-central1}"

STATE_BUCKET="forensic-flight-tfstate"
ENV="${ENV:-prod}"
SECRET_PREFIX="forensic-flight-${ENV}"

echo "=== Phase 1: Terraform state bucket ==="

# Create bucket if it doesn't exist
if ! gsutil ls -b "gs://$STATE_BUCKET" &>/dev/null; then
  gsutil mb -l "$REGION" -b on "gs://$STATE_BUCKET"
  echo "Created gs://$STATE_BUCKET"
else
  echo "Bucket gs://$STATE_BUCKET already exists"
fi

# Enable versioning (state recovery on corruption)
gsutil versioning set on "gs://$STATE_BUCKET"

# Enforce uniform bucket-level access (no per-object ACLs)
gsutil uniformbucketlevelaccess set on "gs://$STATE_BUCKET"

# Enforce no public access
gsutil pap set enforced "gs://$STATE_BUCKET"

echo "State bucket hardened."

echo ""
echo "=== Phase 2: Populate Secret Manager values ==="
echo "Run terraform apply first to create the secret containers, then run this"
echo "section to populate values. Re-run after secrets rotate."
echo ""
echo "Commands (run after terraform apply):"
echo ""
echo "  # Generate strong secrets if you don't have values:"
echo "  DB_PASSWORD=\$(openssl rand -base64 32 | tr -d '=+/' | head -c 40)"
echo "  REDIS_PASSWORD=\$(openssl rand -base64 32 | tr -d '=+/' | head -c 40)"
echo "  API_SECRET_KEY=\$(openssl rand -base64 48)"
echo ""
echo "  printf '%s' \"\$DB_PASSWORD\" | gcloud secrets versions add ${SECRET_PREFIX}-db-password --data-file=- --project=${PROJECT_ID}"
echo "  printf '%s' \"\$REDIS_PASSWORD\" | gcloud secrets versions add ${SECRET_PREFIX}-redis-password --data-file=- --project=${PROJECT_ID}"
echo "  printf '%s' \"\$API_SECRET_KEY\" | gcloud secrets versions add ${SECRET_PREFIX}-api-secret-key --data-file=- --project=${PROJECT_ID}"
echo "  printf '%s' \"\$ANTHROPIC_API_KEY\" | gcloud secrets versions add ${SECRET_PREFIX}-anthropic-api-key --data-file=- --project=${PROJECT_ID}"
echo "  printf '%s' \"\$OPENAI_API_KEY\" | gcloud secrets versions add ${SECRET_PREFIX}-openai-api-key --data-file=- --project=${PROJECT_ID}"
echo ""
echo "  IMPORTANT: Store DB_PASSWORD and REDIS_PASSWORD in a password manager."
echo "  You will need DB_PASSWORD again if you ever recreate Cloud SQL."
echo ""
echo "=== Phase 3: Initialize Terraform ==="
echo ""
echo "  cd infra/gcp/terraform"
echo "  terraform init"
echo "  terraform plan -out=tfplan"
echo "  # Review plan carefully before apply"
echo "  terraform apply tfplan"
echo ""
echo "=== Phase 4: Populate secrets (after apply) ==="
echo "  Run the gcloud secrets commands above after terraform apply creates the containers."
echo ""
echo "=== Bootstrap complete ==="
