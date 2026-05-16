# ── Notification channel (email) ──────────────────────────────────────────────

resource "google_monitoring_notification_channel" "email" {
  display_name = "Forensic Flight Alert Email"
  type         = "email"

  labels = {
    email_address = var.alert_email
  }
}

# ── Alert: Cloud Run API high error rate ──────────────────────────────────────

resource "google_monitoring_alert_policy" "api_error_rate" {
  display_name = "[${var.env}] Cloud Run API 5xx count > 10 in 5 min"
  combiner     = "OR"

  conditions {
    display_name = "More than 10 5xx errors in any 5-minute window"

    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${var.api_service_name}\" AND metric.type=\"run.googleapis.com/request_count\" AND metric.labels.response_code_class=\"5xx\""
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = 10

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_COUNT"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  alert_strategy {
    auto_close = "604800s"
  }
}

# ── Alert: Cloud Run API high latency ─────────────────────────────────────────

resource "google_monitoring_alert_policy" "api_latency" {
  display_name = "[${var.env}] Cloud Run API p95 latency > 5s"
  combiner     = "OR"

  conditions {
    display_name = "p95 latency > 5000ms for 5 min"

    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${var.api_service_name}\" AND metric.type=\"run.googleapis.com/request_latencies\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 5000

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MEAN"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  alert_strategy {
    auto_close = "604800s"
  }
}

# ── Budget alert ──────────────────────────────────────────────────────────────

resource "google_billing_budget" "main" {
  billing_account = var.billing_account_id
  display_name    = "Forensic Flight ${var.env} monthly budget"

  budget_filter {
    projects = ["projects/${var.project_id}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(floor(var.monthly_budget_usd))
    }
  }

  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 0.8
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }

  all_updates_rule {
    monitoring_notification_channels = [google_monitoring_notification_channel.email.id]
    disable_default_iam_recipients   = false
  }
}
