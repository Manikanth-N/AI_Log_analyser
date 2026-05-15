# ── AWS Budget alarm ──────────────────────────────────────────────────────────
# Triggers SNS alert when monthly spend exceeds threshold.
# Does NOT stop resources (cost control only — add budget action for that).

variable "monthly_budget_usd" {
  type        = number
  default     = 500  # alert at $500/mo; adjust to 2× your expected normal spend
  description = "Monthly spend threshold in USD before alert fires."
}

resource "aws_budgets_budget" "monthly" {
  name         = "${local.name_prefix}-monthly-budget"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$forensic-flight"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80   # alert at 80% of budget
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100  # alert when budget is exceeded
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100  # forecasted overrun alert
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
  }
}

# ── LLM spend anomaly detection ───────────────────────────────────────────────
# Alerts when ECS investigation worker CPU drops unexpectedly (possible runaway inference).
# Pair this with application-level token accounting in InferenceClient.

resource "aws_cloudwatch_metric_alarm" "investigation_queue_depth" {
  alarm_name          = "${local.name_prefix}-investigate-queue-depth"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesNotVisible"
  namespace           = "AWS/SQS"   # Uses Redis queue depth if published to CW
  period              = 300
  statistic           = "Maximum"
  threshold           = 10
  alarm_description   = "More than 10 investigations queued — possible backlog or stuck worker"
  treat_missing_data  = "notBreaching"

  # Publish investigation queue depth as a custom metric from the worker
  # (requires a CloudWatch agent or custom metric publisher — see deploy/runbook.md)
  dimensions = {}   # placeholder; replace with actual dimension when custom metric is set up

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# ── NAT Gateway data transfer alarm ──────────────────────────────────────────
# Catches runaway LLM API calls (tokens × price per call × call rate).
# NAT Gateway bytes processed is a proxy for outbound API traffic.

resource "aws_cloudwatch_metric_alarm" "nat_bytes_high" {
  alarm_name          = "${local.name_prefix}-nat-bytes-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "BytesOutToDestination"
  namespace           = "AWS/NATGateway"
  period              = 3600
  statistic           = "Sum"
  threshold           = 1073741824  # 1 GB/hour — adjust to your expected API call volume
  alarm_description   = "NAT Gateway outbound > 1GB/hour — possible runaway LLM API calls"
  treat_missing_data  = "notBreaching"

  dimensions = {
    NatGatewayId = aws_nat_gateway.main.id
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# ── S3 storage growth alarm ───────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "s3_data_bucket_size" {
  alarm_name          = "${local.name_prefix}-s3-data-size-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "BucketSizeBytes"
  namespace           = "AWS/S3"
  period              = 86400   # daily metric (S3 reports once per day)
  statistic           = "Average"
  threshold           = 107374182400  # 100 GB
  alarm_description   = "Data bucket exceeded 100 GB — review log retention policy"
  treat_missing_data  = "notBreaching"

  dimensions = {
    BucketName  = aws_s3_bucket.data.id
    StorageType = "StandardStorage"
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}
