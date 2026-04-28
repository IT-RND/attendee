from bots.models import Project, ZoomOAuthApp
from bots.tasks import validate_zoom_oauth_connections


def create_or_update_zoom_oauth_app(project: Project, client_id: str, client_secret: str, webhook_secret: str) -> tuple[ZoomOAuthApp | None, str | None]:
    zoom_oauth_app = ZoomOAuthApp.objects.filter(project=project).first()

    client_secret = (client_secret or "").strip()
    webhook_secret = (webhook_secret or "").strip()

    if not zoom_oauth_app:
        # Creating new app - client_id and client_secret are required
        if not client_id or not client_secret:
            return None, "client_id and client_secret are required when creating a new Zoom OAuth app"

        zoom_oauth_app = ZoomOAuthApp(project=project, client_id=client_id)
        zoom_oauth_app.set_credentials({"client_secret": client_secret, "webhook_secret": webhook_secret})
        return zoom_oauth_app, None
    else:
        # Updating existing app - only update secrets if provided
        existing_credentials = zoom_oauth_app.get_credentials() or {}

        # If the client_secret changed, validate the zoom oauth connections associated with this app.
        # Zoom user OAuth app credentials are validated during the authorization-code exchange.
        # Since they might have been disconnected due to the previous client_secret being invalid
        if client_secret and client_secret != zoom_oauth_app.client_secret:
            validate_zoom_oauth_connections.delay(zoom_oauth_app.id)

        # Build updated credentials dict, preserving existing values if new ones are blank
        updated_credentials = {"client_secret": client_secret if client_secret else existing_credentials.get("client_secret", ""), "webhook_secret": webhook_secret if webhook_secret else existing_credentials.get("webhook_secret", "")}
        zoom_oauth_app.set_credentials(updated_credentials)

        return zoom_oauth_app, None
