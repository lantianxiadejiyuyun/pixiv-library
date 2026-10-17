"""Browser access uses the same configured origins as write verification."""
from starlette.middleware.cors import CORSMiddleware

from .config import settings


class LibraryCORSMiddleware(CORSMiddleware):
    def __init__(self, app):
        # Reflect each accepted origin, including the first cookie-free login.
        # Credentialed CORS cannot use Access-Control-Allow-Origin: *.
        super().__init__(app, allow_origins=(), allow_credentials=True,
            allow_methods=["*"], allow_headers=["*"],
            expose_headers=["Content-Disposition", "Content-Range", "Accept-Ranges"])
        self.allow_private_network = True

    def is_allowed_origin(self, origin):
        return "*" in settings.allowed_origins or origin.rstrip("/") in settings.allowed_origins

    def preflight_response(self, request_headers):
        response = super().preflight_response(request_headers)
        # Also works with Starlette releases predating the private-network option.
        if response.status_code == 200 and request_headers.get("access-control-request-private-network") == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response
