"""Google Calendar OAuth2 endpoints.

Endpoints:
  GET  /auth/google/start    — Redirect user to Google's consent screen
  GET  /auth/google/callback — Exchange code for tokens, store refresh token
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)


def build_google_auth_router(
    calendar_service,
    user_repo,
) -> APIRouter:
    """Factory: builds the Google OAuth router with injected dependencies."""
    router = APIRouter(prefix="/auth/google", tags=["google-auth"])

    @router.get("/start")
    async def start_auth(phone: str = Query(..., description="User phone in E.164")) -> RedirectResponse:
        """Redirect the user to Google's OAuth2 consent page.

        The `phone` query param identifies which user is authorizing.
        It's passed as `state` so we can link the token back to the user.
        """
        auth_url = calendar_service.build_auth_url(state=phone)
        return RedirectResponse(url=auth_url)

    @router.get("/callback")
    async def auth_callback(
        request: Request,
        code: str = Query(...),
        state: str = Query(default=""),
    ) -> HTMLResponse:
        """Google redirects here after user consents.

        Exchange the code for tokens and store the refresh token.
        """
        phone = state
        if not phone:
            return HTMLResponse(
                content="<h1>Error</h1><p>Missing user identifier (state).</p>",
                status_code=400,
            )

        try:
            tokens = await calendar_service.exchange_code(code)
        except Exception:
            logger.exception("Failed to exchange Google OAuth code")
            return HTMLResponse(
                content="<h1>Error</h1><p>Failed to authorize with Google. Please try again.</p>",
                status_code=500,
            )

        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            return HTMLResponse(
                content=(
                    "<h1>Error</h1>"
                    "<p>No refresh token received. Please revoke access at "
                    '<a href="https://myaccount.google.com/permissions">Google Permissions</a> '
                    "and try again.</p>"
                ),
                status_code=400,
            )

        # Store the refresh token on the user
        user = await user_repo.get_by_phone(phone)
        if user is None:
            return HTMLResponse(
                content=f"<h1>Error</h1><p>User with phone {phone} not found.</p>",
                status_code=404,
            )

        user.google_calendar_token = refresh_token
        await user_repo.save(user)
        logger.info("Stored Google Calendar refresh token for %s", phone)

        return HTMLResponse(
            content=(
                "<html><body style='font-family: sans-serif; text-align: center; padding: 60px;'>"
                "<h1>Google Calendar Connected</h1>"
                "<p>Your calendar has been linked successfully. "
                "You can close this window and return to WhatsApp.</p>"
                "</body></html>"
            ),
            status_code=200,
        )

    return router
