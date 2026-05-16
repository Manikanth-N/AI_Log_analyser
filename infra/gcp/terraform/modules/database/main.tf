# Cloud SQL — PostgreSQL 15, db-g1-small, private IP only

resource "google_sql_database_instance" "main" {
  name             = "${local.prefix}-postgres"
  database_version = "POSTGRES_15"
  region           = var.region

  deletion_protection = true

  settings {
    tier              = "db-g1-small"
    availability_type = "ZONAL"
    disk_size         = 20
    disk_type         = "PD_SSD"
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = var.vpc_id
      enable_private_path_for_google_cloud_services = true
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "03:00"
      point_in_time_recovery_enabled = false
      backup_retention_settings {
        retained_backups = 7
        retention_unit   = "COUNT"
      }
    }

    maintenance_window {
      day          = 7
      hour         = 4
      update_track = "stable"
    }

    database_flags {
      name  = "max_connections"
      value = "100"
    }

    insights_config {
      query_insights_enabled = true
    }
  }

  depends_on = [var.private_ip_peering_dependency]
}

resource "google_sql_database" "forensic_flight" {
  name     = "forensic_flight"
  instance = google_sql_database_instance.main.name
}

resource "google_sql_user" "app" {
  name     = "forensic_flight"
  instance = google_sql_database_instance.main.name
  password = var.db_password
}
