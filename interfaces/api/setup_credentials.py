"""HTTPS endpoints for the credentials magic-link form.

Renders a minimal self-contained HTML form (no JS, no external assets) so
there's nothing to bundle and no client-side surface area. Credentials never
hit the templating layer twice — we read them off the request and pass them
straight to the use case.
"""
from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse

from application.setup_credentials import (
    SetupCredentialsUseCase,
    SubmissionResult,
    TokenStatus,
)

logger = logging.getLogger(__name__)


def build_setup_credentials_router(
    use_case: SetupCredentialsUseCase,
    path: str = "/setup/credentials",
) -> APIRouter:
    router = APIRouter(tags=["setup"])

    @router.get(path, response_class=HTMLResponse)
    async def show_form(token: str = Query(..., min_length=10)) -> HTMLResponse:
        validation = await use_case.validate_token(token)
        if validation.status != TokenStatus.OK:
            return HTMLResponse(_render_error_page(validation.status), status_code=400)
        # Don't leak the phone — just the token + a friendly name if present
        name = validation.draft.name if validation.draft else None
        return HTMLResponse(_render_form(token=token, path=path, name=name))

    @router.post(path, response_class=HTMLResponse)
    async def submit_form(
        token: str = Form(...),
        nie: str = Form(default=""),
        password: str = Form(default=""),
    ) -> HTMLResponse:
        outcome = await use_case.submit_credentials(token, nie=nie, password=password)
        if outcome.result == SubmissionResult.OK:
            return HTMLResponse(_render_success_page())
        return HTMLResponse(
            _render_submission_error(outcome.result), status_code=400
        )

    return router


# ---- HTML rendering ----

_BASE_STYLES = """
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f6fa; margin: 0; padding: 2rem; color: #1f2937; }
  .card { max-width: 420px; margin: 4rem auto; background: white; padding: 2rem;
          border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,0.06); }
  h1 { margin-top: 0; font-size: 1.4rem; }
  p { line-height: 1.5; }
  label { display: block; margin-top: 1rem; font-weight: 600; font-size: 0.9rem; }
  input[type=text], input[type=password] {
    width: 100%; padding: 0.6rem; border: 1px solid #d1d5db; border-radius: 6px;
    margin-top: 0.25rem; box-sizing: border-box; font-size: 1rem; }
  button { margin-top: 1.5rem; width: 100%; background: #2563eb; color: white;
           padding: 0.7rem; border: none; border-radius: 6px; font-size: 1rem;
           cursor: pointer; }
  button:hover { background: #1d4ed8; }
  .muted { color: #6b7280; font-size: 0.85rem; }
  .ok { color: #047857; }
  .err { color: #b91c1c; }
"""


def _page(body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MedAgent — Connect your Cigna account</title>
<style>{_BASE_STYLES}</style>
</head><body>{body}</body></html>"""


def _render_form(*, token: str, path: str, name: str | None) -> str:
    safe_token = html.escape(token)
    safe_action = html.escape(path)
    greeting = f"Hi {html.escape(name.split()[0])}! " if name else ""
    body = f"""
<div class="card">
  <h1>Connect your Cigna account</h1>
  <p>{greeting}Enter your Cigna portal credentials so the agent can log in on
  your behalf. They're encrypted at rest and never shown again.</p>
  <form method="post" action="{safe_action}" autocomplete="off">
    <input type="hidden" name="token" value="{safe_token}">
    <label for="nie">NIE / NIF / Passport</label>
    <input type="text" id="nie" name="nie" required autocomplete="off">
    <label for="password">Cigna password</label>
    <input type="password" id="password" name="password" required autocomplete="new-password">
    <button type="submit">Connect</button>
  </form>
  <p class="muted">This link is single-use and expires 10 minutes after we
  sent it.</p>
</div>
"""
    return _page(body)


def _render_error_page(status: TokenStatus) -> str:
    messages = {
        TokenStatus.NOT_FOUND: "This link isn't valid. Send a new WhatsApp message to start over.",
        TokenStatus.EXPIRED: "This link has expired. Send a new WhatsApp message to get a fresh one.",
        TokenStatus.ALREADY_USED: "This link has already been used.",
    }
    msg = messages.get(status, "We couldn't load this page.")
    body = f"""
<div class="card">
  <h1>Link unavailable</h1>
  <p class="err">{html.escape(msg)}</p>
</div>
"""
    return _page(body)


def _render_submission_error(result: SubmissionResult) -> str:
    messages = {
        SubmissionResult.INVALID_TOKEN: "This link isn't valid anymore.",
        SubmissionResult.EXPIRED_TOKEN: "This link expired before you submitted. Request a new one over WhatsApp.",
        SubmissionResult.USED_TOKEN: "This link has already been used.",
        SubmissionResult.DRAFT_MISSING: (
            "Looks like your onboarding session expired. Send another "
            "WhatsApp message to restart."
        ),
        SubmissionResult.BAD_INPUT: "Both fields are required.",
    }
    msg = messages.get(result, "Something went wrong.")
    body = f"""
<div class="card">
  <h1>We couldn't save that</h1>
  <p class="err">{html.escape(msg)}</p>
</div>
"""
    return _page(body)


def _render_success_page() -> str:
    body = """
<div class="card">
  <h1>You're connected.</h1>
  <p class="ok">Your Cigna account is linked. You can close this tab — head
  back to WhatsApp and tell me when you need a doctor.</p>
</div>
"""
    return _page(body)
