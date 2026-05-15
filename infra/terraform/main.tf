terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state: create this S3 bucket + DynamoDB table manually before
  # running terraform init (see deploy/runbook.md bootstrap section).
  backend "s3" {
    bucket         = "forensic-flight-tfstate"
    key            = "prod/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "forensic-flight-tfstate-lock"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "forensic-flight"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# Random suffix for globally unique S3 bucket names
resource "random_id" "suffix" {
  byte_length = 4
}

locals {
  name_prefix = "forensic-flight-${var.environment}"
  suffix      = random_id.suffix.hex

  # Application container images (populated by CI/CD after ECR push)
  api_image    = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"
  worker_image = "${aws_ecr_repository.worker.repository_url}:${var.image_tag}"
}
