resource "google_pubsub_topic" "pedidos" {
  name = "${var.prefijo}-pedidos"

  depends_on = [google_project_service.apis]
}

# El backend puede publicar en este tema y en ningún otro.
resource "google_pubsub_topic_iam_member" "backend_publica" {
  topic  = google_pubsub_topic.pedidos.name
  role   = "roles/pubsub.publisher"
  member = google_service_account.backend.member
}

# ---------- Cocina ----------

resource "google_service_account" "cocina" {
  account_id   = "${var.prefijo}-cocina"
  display_name = "Cocina de la cafetería"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "cocina_firestore" {
  project = var.proyecto
  role    = "roles/datastore.user"
  member  = google_service_account.cocina.member
}

# Sin un google_cloud_run_v2_service_iam_member con allUsers: esa ausencia es
# lo que hace privada a la cocina.
resource "google_cloud_run_v2_service" "cocina" {
  name                = "${var.prefijo}-cocina"
  location            = var.region
  deletion_protection = false

  template {
    service_account = google_service_account.cocina.email

    scaling {
      max_instance_count = 3
    }

    # Tantas peticiones a la vez como hilos tiene gunicorn en la cocina.
    max_instance_request_concurrency = 8

    containers {
      image = var.imagen_cocina

      env {
        name  = "BASE_DATOS"
        value = google_firestore_database.cafeteria.name
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_project_iam_member.cocina_firestore,
  ]
}
