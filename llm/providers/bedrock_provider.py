"""
AWS Bedrock inference provider — SCAFFOLD (not yet implemented).

When to use:
  - Enterprise customers requiring VPC PrivateLink (traffic never hits public internet)
  - Government / regulated workloads needing AWS BAA, FedRAMP, ITAR compliance
  - Customers already on AWS with existing Bedrock commitments

Implementation path:
  pip install boto3 anthropic[bedrock]

  from anthropic import AnthropicBedrock
  import instructor

  client = AnthropicBedrock(
      aws_region="us-east-1",
      # auth via boto3 credentials: IAM role, env vars, or ~/.aws/credentials
  )
  instructor_client = instructor.from_anthropic(client)
  # Same interface as AnthropicProvider from here

Bedrock model IDs (as of mid-2026):
  "anthropic.claude-sonnet-4-6-20250514-v1:0"
  "meta.llama3-3-70b-instruct-v1:0"
  "mistral.mistral-large-2402-v1:0"

VPC PrivateLink configuration:
  Set endpoint URL to the VPC endpoint, not the public Bedrock endpoint.
  Ensure your EC2 / ECS task role has bedrock:InvokeModel permission.

  IAM policy (minimal):
  {
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-*"
  }
"""

from __future__ import annotations

from .base import TokenUsage


class BedrockProvider:
    """AWS Bedrock — scaffold. Raise NotImplementedError until implemented."""

    @property
    def provider_id(self) -> str:
        return "bedrock"

    @property
    def default_model(self) -> str:
        return "anthropic.claude-sonnet-4-6-20250514-v1:0"

    def structured(self, *args, **kwargs):
        raise NotImplementedError(
            "BedrockProvider is not yet implemented. "
            "See llm/providers/bedrock_provider.py for the implementation guide."
        )

    def complete(self, *args, **kwargs):
        raise NotImplementedError("BedrockProvider is not yet implemented.")

    def last_usage(self) -> TokenUsage:
        return TokenUsage()
