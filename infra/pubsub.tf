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
      max_instance_count = var.max_instancias_cocina
    }

    # Tantas peticiones a la vez como hilos tiene gunicorn en la cocina.
    max_instance_request_concurrency = 8

    containers {
      image = var.imagen_cocina

      env {
        name  = "BASE_DATOS"
        value = google_firestore_database.cafeteria.name
      }

      env {
        name  = "PROBABILIDAD_FALLO"
        value = var.probabilidad_fallo
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_project_iam_member.cocina_firestore,
  ]
}

# ---------- Cuenta invocadora ----------

# El número del proyecto (no su ID) está en el correo del agente de Pub/Sub.
data "google_project" "actual" {}

locals {
  agente_pubsub = "serviceAccount:service-${data.google_project.actual.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_service_account" "invocador" {
  account_id   = "${var.prefijo}-invocador"
  display_name = "Pub/Sub invoca a la cocina"

  depends_on = [google_project_service.apis]
}

# La cuenta invocadora puede llamar a la cocina y a nada más.
resource "google_cloud_run_v2_service_iam_member" "invocador_cocina" {
  name     = google_cloud_run_v2_service.cocina.name
  location = google_cloud_run_v2_service.cocina.location
  role     = "roles/run.invoker"
  member   = google_service_account.invocador.member
}

# El agente de Pub/Sub firma los tokens OIDC a nombre de la cuenta invocadora.
resource "google_service_account_iam_member" "pubsub_firma_tokens" {
  service_account_id = google_service_account.invocador.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.agente_pubsub

  # El agente de Pub/Sub aparece cuando se usa la API por primera vez.
  depends_on = [google_pubsub_topic.pedidos]
}

# ---------- Suscripción ----------

# Pub/Sub le hace un POST a la cocina con cada mensaje, firmado como la cuenta
# invocadora. La cocina tarda 6 s en preparar: 60 s de plazo evita reentregas.
resource "google_pubsub_subscription" "cocina" {
  name  = "${var.prefijo}-cocina"
  topic = google_pubsub_topic.pedidos.id

  ack_deadline_seconds = 60

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "60s"
  }

  # Tras 10 entregas fallidas el mensaje se va al tema de muertos. Con la
  # espera entre reintentos, 10 intentos le dan a un pedido varios minutos para
  # superar un problema pasajero antes de rendirse.
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.muertos.id
    max_delivery_attempts = 10
  }

  push_config {
    push_endpoint = google_cloud_run_v2_service.cocina.uri

    oidc_token {
      service_account_email = google_service_account.invocador.email
      audience              = google_cloud_run_v2_service.cocina.uri
    }
  }

  depends_on = [
    google_cloud_run_v2_service_iam_member.invocador_cocina,
    google_service_account_iam_member.pubsub_firma_tokens,
    google_pubsub_topic_iam_member.pubsub_publica_muertos,
  ]
}

# ---------- Mensajes muertos ----------

resource "google_pubsub_topic" "muertos" {
  name = "${var.prefijo}-pedidos-muertos"

  depends_on = [google_project_service.apis]
}

# Sin una suscripción, el tema descarta lo que le llega y el mensaje muerto se pierde.
resource "google_pubsub_subscription" "muertos" {
  name  = "${var.prefijo}-muertos"
  topic = google_pubsub_topic.muertos.id
}

# El agente de Pub/Sub es quien mueve el mensaje: lo publica en el tema de muertos...
resource "google_pubsub_topic_iam_member" "pubsub_publica_muertos" {
  topic  = google_pubsub_topic.muertos.name
  role   = "roles/pubsub.publisher"
  member = local.agente_pubsub
}

# ...y lo confirma en la suscripción de la cocina para que deje de reintentarse.
resource "google_pubsub_subscription_iam_member" "pubsub_confirma_cocina" {
  subscription = google_pubsub_subscription.cocina.name
  role         = "roles/pubsub.subscriber"
  member       = local.agente_pubsub
}
