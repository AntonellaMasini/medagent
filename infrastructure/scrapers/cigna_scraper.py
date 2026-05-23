"""Cigna Spain cuadro médico scraper.

Architecture (see CLAUDE.md): **browser for auth, API for actions.**

  1. Playwright opens a Chromium context, logs the user in with their NIE +
     password (+ OTP if challenged). All session cookies live on the context.
  2. With those cookies in place, all data fetches go through
     CignaApiClient.fetch_user_context + search_doctors — direct JSON API
     calls, no HTML parsing.
  3. Cookies are persisted to disk so subsequent searches skip the login
     step entirely when possible.

The API gives us structured doctor records with geo coordinates and
distances pre-computed, so this module no longer needs Google Maps to sort
or measure results.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from application.ports import BaseInsurerScraper, OTPProvider, OTPRequired
from domain.entities.doctor import Doctor
from domain.entities.user import User
from domain.value_objects.address import Address, Coordinates
from domain.value_objects.specialty import Specialty
from infrastructure.scrapers.cigna_api_client import CignaApiClient

logger = logging.getLogger(__name__)


_DEFAULT_LOGIN_URL = "https://clientes.cigna.es"
_DEFAULT_DOCTORS_URL = "https://clientes.cigna.es/cp/cuadro-medico"


class CignaScraper(BaseInsurerScraper):
    def __init__(
        self,
        cookies_dir: Path,
        login_url: str = _DEFAULT_LOGIN_URL,
        doctors_url: str = _DEFAULT_DOCTORS_URL,
        headless: bool = True,
        timeout_ms: int = 30000,
        search_limit: int = 20,
    ):
        self._cookies_dir = Path(cookies_dir)
        self._cookies_dir.mkdir(parents=True, exist_ok=True)
        self._login_url = login_url
        self._doctors_url = doctors_url
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._search_limit = search_limit

    # ---- public API ----

    async def find_doctors(
        self,
        user: User,
        specialty: Specialty,
        near: Address,  # noqa: ARG002 — kept for port compatibility; Cigna distance is server-computed
        otp_code: str | None = None,
        wait_for_otp: OTPProvider | None = None,
    ) -> list[Doctor]:
        async with async_playwright() as pw:
            logger.info(
                "Launching Chromium (headless=%s, timeout_ms=%d)",
                self._headless,
                self._timeout_ms,
            )
            browser = await pw.chromium.launch(headless=self._headless)
            logger.info("Chromium launched successfully")
            try:
                cookie_path = self._cookie_path(user.phone)
                has_cached_cookies = cookie_path.exists()
                logger.info(
                    "Cookie path=%s exists=%s", cookie_path, has_cached_cookies
                )
                # Force Spanish locale: Cigna's site language-switches based
                # on browser locale + Accept-Language, and all of our text-
                # based selectors ("Verifícate", "Aceptar todo", "Acceder")
                # assume the Spanish UI. Without this, the page rendered in
                # English and the verify-identity / OTP steps silently
                # skipped because none of the Spanish text matched.
                context = await browser.new_context(
                    storage_state=cookie_path if has_cached_cookies else None,
                    locale="es-ES",
                    extra_http_headers={"Accept-Language": "es-ES,es;q=0.9"},
                )

                # Log every POST/PUT to cigna.es so we can see exactly what
                # the SMS-dispatch request looks like and how the backend
                # responds. Helps diagnose silent server-side rejections.
                def _on_request(request):
                    if (
                        "cigna.es" in request.url
                        and request.method in ("POST", "PUT", "DELETE")
                    ):
                        logger.info(
                            "→ %s %s", request.method, request.url
                        )

                async def _on_response(response):
                    if (
                        "cigna.es" in response.url
                        and response.request.method in ("POST", "PUT", "DELETE")
                    ):
                        body_preview = ""
                        try:
                            body = await response.text()
                            body_preview = body[:300].replace("\n", " ")
                        except Exception:
                            pass
                        logger.info(
                            "← %s %s %s body=%s",
                            response.request.method,
                            response.status,
                            response.url,
                            body_preview,
                        )

                context.on("request", _on_request)
                context.on(
                    "response",
                    lambda r: asyncio.create_task(_on_response(r)),
                )
                # Pre-emptively hide OneTrust's cookie SDK with CSS, AND
                # patch the navigator.webdriver fingerprint that signals
                # automation. Cigna (like most banks/insurers) silently
                # refuses cost-bearing endpoints (SMS dispatch, payments)
                # when navigator.webdriver === true — the request succeeds
                # at the HTTP level but the backend drops it server-side.
                # Both patches run before any page script via add_init_script.
                await context.add_init_script(
                    """
                    // Anti-detection: hide the webdriver flag.
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => false,
                        configurable: true,
                    });
                    // Chrome-on-automation also nulls out window.chrome — restore it.
                    if (!window.chrome) {
                        window.chrome = { runtime: {} };
                    }
                    // Languages/plugins are sometimes inspected too; ensure
                    // they look like a real es-ES Chrome.
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['es-ES', 'es', 'en'],
                    });

                    // Hide OneTrust cookie banner via CSS so it can't
                    // intercept clicks.
                    const style = document.createElement('style');
                    style.textContent = `
                        #onetrust-consent-sdk,
                        #onetrust-banner-sdk,
                        #onetrust-pc-sdk,
                        .onetrust-pc-dark-filter {
                            display: none !important;
                            pointer-events: none !important;
                            visibility: hidden !important;
                        }
                    `;
                    if (document.head) {
                        document.head.appendChild(style);
                    } else {
                        document.addEventListener('DOMContentLoaded',
                            () => document.head.appendChild(style));
                    }
                    """
                )
                context.set_default_timeout(self._timeout_ms)
                page = await context.new_page()

                await self._ensure_logged_in(
                    page, context, user, otp_code, has_cached_cookies,
                    wait_for_otp,
                )

                api = CignaApiClient(context)
                user_ctx = await api.fetch_user_context(
                    user.insurer_credentials.username
                )
                raw_doctors = await api.search_doctors(
                    specialty=specialty,
                    chipcard=user_ctx.chipcard,
                    id_member=user_ctx.id_member,
                    group_code=user_ctx.group_code,
                    limit=self._search_limit,
                )
                return _parse_doctors_response(raw_doctors, specialty)
            finally:
                await browser.close()

    # ---- login / session (unchanged from the previous version) ----

    def _cookie_path(self, phone: str) -> Path:
        safe = re.sub(r"[^0-9A-Za-z]", "_", phone)
        return self._cookies_dir / f"cigna_{safe}.json"

    async def _ensure_logged_in(
        self,
        page: Page,
        context: BrowserContext,
        user: User,
        otp_code: str | None,
        has_cached_cookies: bool,
        wait_for_otp: OTPProvider | None = None,
    ) -> None:
        # Only attempt the "use cached cookies" fast path if we actually have
        # cookies on disk. Without this gate, `_is_authenticated` produces a
        # false positive: Cigna's SPA renders the /cp/cuadro-medico URL shell
        # for everyone (auth state is checked client-side), so the URL+DOM
        # heuristics can't distinguish "logged in" from "shell loading".
        if has_cached_cookies:
            await page.goto(self._doctors_url, wait_until="domcontentloaded")
            if await self._is_authenticated(page):
                logger.info(
                    "Cigna session resumed from cached cookies for %s", user.phone
                )
                return
            logger.info(
                "Cached cookies didn't work for %s; logging in fresh", user.phone
            )

        logger.info("No valid session; logging in fresh for %s", user.phone)
        logger.info("Navigating to login URL: %s", self._login_url)
        await page.goto(self._login_url, wait_until="domcontentloaded")
        logger.info("Login page loaded; current URL: %s", page.url)
        await self._dismiss_cookies_banner(page)
        logger.info("Cookie banner step done; filling login form")
        await self._fill_login_form(page, user)
        # Give the form click a moment to actually trigger a navigation or
        # XHR before checking state. This URL tells us whether the submit
        # worked at all (e.g. /cp/login still = submit was no-op).
        await page.wait_for_timeout(1500)
        logger.info("Login form submitted; URL is now %s", page.url)
        await self._maybe_handle_otp(page, otp_code, wait_for_otp)
        logger.info("OTP handler done; waiting for SPA to leave the login screen")

        # CRUCIAL: do NOT call page.goto() here. After Okta accepts the OTP,
        # Cigna's Angular SPA writes the access token to localStorage and
        # navigates to /cp/cuadro-medico (or wherever) on its own. A manual
        # page.goto would force a full reload, which throws away the SPA's
        # in-memory state — the token hadn't been persisted to localStorage
        # yet, so the new page boots up unauthenticated and bounces back to
        # /cp/login. Wait for the SPA's *own* navigation away from the
        # login screen instead.
        try:
            await page.wait_for_url(
                lambda url: "/cp/login" not in url,
                timeout=20000,
            )
            logger.info("SPA navigated post-auth to: %s", page.url)
        except PlaywrightTimeout:
            logger.warning(
                "SPA stayed on /cp/login for 20s after OTP — auth probably "
                "failed or there's another step we missed"
            )

        # Only navigate explicitly if the SPA didn't already land us on
        # the doctors URL. By now the auth tokens are persisted in
        # localStorage, so a reload is safe.
        if "/cp/cuadro-medico" not in page.url:
            await page.goto(self._doctors_url, wait_until="domcontentloaded")
        logger.info("After login, URL is: %s", page.url)
        if not await self._is_authenticated(page):
            # Capture diagnostic state so we can see what screen we're stuck
            # on (verify-identity? login? something else?) without needing
            # to re-run with headed mode + breakpoints.
            await self._log_auth_failure_diagnostics(page, user.phone)
            raise RuntimeError(
                "Cigna login appeared to succeed but cuadro médico is not accessible"
            )
        await context.storage_state(path=str(self._cookie_path(user.phone)))
        logger.info("Saved Cigna cookies for %s", user.phone)

    async def _is_authenticated(self, page: Page) -> bool:
        """True only if the page actually rendered the authenticated cuadro
        médico view — NOT an IdP login redirect, NOT /cp/home, NOT the
        verify-identity screen.

        The previous version used `"/cp/cuadro-medico" in page.url`, which
        passed wrongly when Cigna redirected us to the IdP at
        login.clientes.cigna.es with `?redirect_uri=...cp/cuadro-medico...`
        in the query string — the substring matches but we're not
        authenticated at all.

        `page.url.startswith(self._doctors_url)` is the correct check: it
        only passes if we genuinely landed on the doctors URL, not somewhere
        with that URL embedded in a query param.
        """
        try:
            if not page.url.startswith(self._doctors_url):
                return False
            if await self._looks_like_login(page):
                return False
            if await self._looks_like_verify_identity(page):
                return False
            return True
        except Exception:
            return False

    async def _looks_like_login(self, page: Page) -> bool:
        login_input = page.locator('input[placeholder*="NIE"]').first
        try:
            return await login_input.is_visible(timeout=1500)
        except PlaywrightTimeout:
            return False

    async def _looks_like_verify_identity(self, page: Page) -> bool:
        try:
            return await page.get_by_text("Verifícate").first.is_visible(
                timeout=1500
            )
        except PlaywrightTimeout:
            return False

    async def _log_auth_failure_diagnostics(self, page: Page, phone: str) -> None:
        """Dump everything useful about the current page state when the post-
        login auth check fails. Helps figure out *which* screen Cigna is
        showing us (verify-identity? fresh login? captcha? English UI?)
        without needing a headed-mode breakpoint.
        """
        try:
            url = page.url
            title = await page.title()
            has_nie_input = (
                await page.locator('input[placeholder*="NIE"]').first.count() > 0
            )
            has_verificate_es = (
                await page.get_by_text("Verifícate").first.count() > 0
            )
            has_verify_en = (
                await page.get_by_text("Verify", exact=False).first.count() > 0
            )
            has_acceder_btn = (
                await page.locator('button:has-text("Acceder")').first.count() > 0
            )
            html_lang = await page.evaluate(
                "() => document.documentElement.lang || '<unset>'"
            )
            # First chunk of visible body text — usually enough to see what
            # the screen actually says.
            body_text_preview = (
                await page.evaluate("() => document.body.innerText")
            )[:500].replace("\n", " | ")
            logger.error(
                "Auth failure diagnostics:\n"
                "  url=%s\n  title=%s\n  html lang=%s\n"
                "  has NIE input=%s  has Acceder button=%s\n"
                "  has 'Verifícate' (ES)=%s  has 'Verify' (EN)=%s\n"
                "  body preview: %s",
                url, title, html_lang,
                has_nie_input, has_acceder_btn,
                has_verificate_es, has_verify_en,
                body_text_preview,
            )
            screenshot_path = (
                Path("infrastructure/cookies")
                / f"cigna_auth_failure_{phone.lstrip('+')}.png"
            )
            await page.screenshot(path=str(screenshot_path), full_page=True)
            logger.error("Saved auth-failure screenshot to %s", screenshot_path)
        except Exception as exc:
            logger.error("Could not gather auth failure diagnostics: %s", exc)

    async def _dismiss_cookies_banner(self, page: Page) -> None:
        # The cookie button isn't always a <button> element — Cigna uses a
        # PrimeNG p-button wrapper that may render as a <span> or <a>.
        # Try (in order): role-based (accessibility tree), bare-text, and
        # OneTrust's stable ID as last resort.
        dismissed = False
        candidates = (
            ("role=Aceptar todas las cookies",
             page.get_by_role("button", name="Aceptar todas las cookies")),
            ("role=Aceptar todas",
             page.get_by_role("button", name="Aceptar todas")),
            ("text=Aceptar todas las cookies",
             page.get_by_text("Aceptar todas las cookies", exact=False)),
            ("id=#onetrust-accept-btn-handler",
             page.locator("#onetrust-accept-btn-handler")),
        )
        for label, locator in candidates:
            try:
                if await locator.first.is_visible(timeout=5000):
                    await locator.first.click()
                    logger.info("Dismissed cookie banner via %s", label)
                    dismissed = True
                    break
            except PlaywrightTimeout:
                continue

        # Wait for the OneTrust SDK to actually hide its overlay. If we
        # found a button, this confirms the dismiss took effect. If we
        # didn't, this gives the page a moment to settle before we nuke.
        try:
            await page.locator("#onetrust-consent-sdk").wait_for(
                state="hidden", timeout=5000
            )
        except PlaywrightTimeout:
            pass

        if not dismissed:
            logger.warning(
                "Cookie banner not found via known selectors; falling back "
                "to DOM removal (OneTrust may still block events globally)"
            )

        # Belt-and-suspenders: remove any lingering OneTrust SDK containers.
        # OneTrust attaches global event listeners that can swallow form-
        # submission keypresses until consent is recorded, so killing the
        # containers protects against that even when dismissal worked.
        await page.evaluate(
            """() => {
                for (const id of [
                    'onetrust-consent-sdk',
                    'onetrust-banner-sdk',
                    'onetrust-pc-sdk',
                ]) {
                    const el = document.getElementById(id);
                    if (el) el.remove();
                }
                document.querySelectorAll(
                    '.onetrust-pc-dark-filter'
                ).forEach(el => el.remove());
            }"""
        )

    async def _fill_login_form(self, page: Page, user: User) -> None:
        nie_input = page.locator('input[placeholder*="NIE"]').first
        pw_input = page.locator('input[placeholder*="Contraseña"]').first
        await nie_input.wait_for(state="visible")
        await nie_input.fill(user.insurer_credentials.username)
        await pw_input.fill(user.insurer_credentials.password)
        # Verify the fill actually took. Cigna's Angular reactive form
        # occasionally fails to register Playwright's fill() (overlay
        # interception, focus loss); the read-back catches that without
        # needing a screenshot.
        nie_value = await nie_input.input_value()
        pw_length = len(await pw_input.input_value())
        logger.info(
            "Post-fill state: nie_input=%r password_length=%d",
            nie_value, pw_length,
        )
        # Native Playwright click — produces an isTrusted=true event,
        # which Cigna's anti-bot checks require. The OneTrust overlay is
        # display:none via our init_script, so it can't intercept.
        await page.get_by_role("button", name="Acceder").first.click()

    async def _maybe_handle_otp(
        self,
        page: Page,
        otp_code: str | None,
        wait_for_otp: OTPProvider | None = None,
    ) -> None:
        """Handle Cigna's two-stage 2FA flow after NIE+password is submitted.

        Stage A — verify-identity screen ("Verifícate para acceder a Mi Cigna"):
          Cigna offers Email and Phone (SMS) options, each with its own
          `Seleccionar` button. We pick SMS because the user already gets
          our prompts on WhatsApp; same device, easy to forward the code.

        Stage B — OTP entry screen:
          - If `otp_code` was passed in upfront, fill it and submit.
          - Else if `wait_for_otp` callback was provided, call it inline.
            The browser stays open while we await the user's reply, which
            is REQUIRED for Cigna/Okta: the OTP is bound to the session
            that requested it; a fresh browser invalidates the code.
          - Else raise OTPRequired (legacy path; do not use with Cigna —
            see find_doctors docstring).

        Either or both stages can be skipped — Cigna sometimes shows a
        cached session that bypasses verification entirely.
        """
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeout:
            pass

        # ---- Stage A: choose SMS on the verify-identity screen ----
        await self._select_sms_verification(page)

        # Let the page transition after the click.
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeout:
            pass

        # ---- Stage A.5: confirm SMS dispatch ----
        # Cigna shows a confirmation screen ("Te vamos a enviar el código…")
        # with an "Enviar Código" button. The SMS is NOT actually sent until
        # this button is clicked — selecting "Phone" only opens this screen.
        await self._click_enviar_codigo(page)

        # ---- Stage B: enter the SMS code ----
        # Broadened selector list (Cigna's OTP input may not have any of the
        # standard otp/code attributes — fall back to any single text input
        # with a numeric/code-y placeholder, or a maxlength of 6).
        otp_input = page.locator(
            'input[autocomplete="one-time-code"], '
            'input[name*="otp" i], '
            'input[name*="codigo" i], '
            'input[name*="code" i], '
            'input[placeholder*="código" i], '
            'input[placeholder*="code" i], '
            'input[inputmode="numeric"], '
            'input[maxlength="6"]'
        ).first

        # Wait up to 20s — Cigna's SMS dispatch + screen transition can be
        # slow on a cold session. wait_for actually blocks until visible;
        # is_visible(timeout=…) does NOT — it returns False immediately if
        # the element isn't in the DOM yet.
        try:
            await otp_input.wait_for(state="visible", timeout=20000)
            visible = True
        except PlaywrightTimeout:
            visible = False

        if not visible:
            # Snapshot whatever IS on screen so we can identify the OTP
            # input's real selector and add it to the list above.
            await self._capture_screen(page, "cigna_otp_no_input.png")
            logger.warning(
                "OTP input not found after 15s — see cigna_otp_no_input.png"
            )
            return  # no OTP screen — login proceeded directly

        if not otp_code:
            if wait_for_otp is None:
                logger.info("Cigna OTP screen detected; signalling OTPRequired")
                raise OTPRequired()
            logger.info(
                "Cigna OTP screen detected; awaiting code from user "
                "(browser will stay open)"
            )
            otp_code = await wait_for_otp()
            if not otp_code:
                logger.warning("OTP wait timed out or no code received")
                raise OTPRequired()
            logger.info("Got OTP code from user; filling and submitting")

        await otp_input.fill(otp_code)

        # Find the submit button. Cigna may use any of these labels.
        submit_btn = None
        for label in ("Verificar", "Continuar", "Validar", "Enviar", "Acceder"):
            btn = page.locator(f'button:has-text("{label}")').first
            try:
                if await btn.is_visible(timeout=1500):
                    submit_btn = btn
                    logger.info("OTP submit button: %r", label)
                    break
            except PlaywrightTimeout:
                continue

        # Wait for Cigna/Okta to ACK the OTP submission before letting the
        # caller navigate away. Same XHR-tear-down hazard as the Enviar
        # Código step: the click fires an async POST to /idp/idx/answer or
        # /idp/idx/challenge/answer (Okta's OTP verification endpoint); if
        # we navigate before that completes, the session never gets the
        # authenticated cookie and we bounce back to /cp/login.
        try:
            async with page.expect_response(
                lambda r: (
                    "/idp/idx/" in r.url
                    and r.request.method == "POST"
                    and ("answer" in r.url or "challenge" in r.url)
                ),
                timeout=15000,
            ) as resp_info:
                if submit_btn is not None:
                    await submit_btn.click()
                else:
                    # Fall back to pressing Enter on the OTP input.
                    await otp_input.press("Enter")
            response = await resp_info.value
            body = await response.text()
            logger.info(
                "OTP submit response: status=%d body=%s",
                response.status, body[:600],
            )
        except PlaywrightTimeout:
            logger.warning(
                "Did not observe OTP submission response within 15s — "
                "auth may not have completed"
            )

    async def _select_sms_verification(self, page: Page) -> None:
        """If the verify-identity screen is up, click `Seleccionar` next to the
        Phone (SMS) option. No-op if the screen isn't present.

        Cigna's verify-identity screen shows two rows (Email and Phone), each
        with its own `Seleccionar` button. We find the Phone row by looking
        for a container whose text mentions "Phone" or matches a Spanish
        phone-number pattern, then click that container's button.
        """
        try:
            visible = await page.get_by_text("Verifícate").first.is_visible(
                timeout=2000
            )
        except PlaywrightTimeout:
            visible = False

        if not visible:
            return  # verify-identity screen not present — skip

        logger.info("Cigna verify-identity screen detected; selecting SMS")

        # The verify-identity screen renders in stages: the "Verifícate"
        # header appears first, then the Email/Phone option rows load
        # asynchronously. Wait for at least one Seleccionar button before
        # trying to count or click. get_by_role catches both <button> and
        # PrimeNG p-button wrappers (which render with role=button but
        # aren't always <button> elements).
        sel_buttons = page.get_by_role("button", name="Seleccionar")
        try:
            await sel_buttons.first.wait_for(state="visible", timeout=8000)
        except PlaywrightTimeout:
            await self._capture_screen(page, "cigna_verify_no_buttons.png")
            logger.warning(
                "No Seleccionar button visible on verify-identity screen "
                "after 8s — see cigna_verify_no_buttons.png"
            )
            return

        # Wait for the second option to render too (Email is typically first,
        # Phone second — we want the Phone row).
        for _ in range(20):  # ~4s of polling
            if await sel_buttons.count() >= 2:
                break
            await page.wait_for_timeout(200)

        count = await sel_buttons.count()
        logger.info("Found %d Seleccionar button(s) on verify-identity", count)

        # Native Playwright click (isTrusted=true, important for Cigna's
        # anti-bot detection on the SMS-dispatch chain). Phone is the 2nd
        # row (index 1); fall back to index 0 if only one option exists.
        target_index = 1 if count >= 2 else 0
        await sel_buttons.nth(target_index).click()
        logger.info(
            "Clicked SMS Seleccionar (native click, index %d of %d)",
            target_index, count,
        )
        return

        await self._capture_screen(page, "cigna_verify_no_buttons.png")
        logger.warning(
            "Could not find SMS Seleccionar button — see "
            "cigna_verify_no_buttons.png; login will likely fail"
        )

    async def _click_enviar_codigo(self, page: Page) -> None:
        """Click "Enviar Código" on the SMS confirmation screen that Cigna
        shows between selecting "Phone" and the OTP entry screen. The SMS
        is only dispatched after this click — without it, no code arrives.
        """
        # Wait for the button to appear (the screen renders after a brief
        # transition from the verify-identity screen).
        button = page.get_by_role("button", name="Enviar Código")
        try:
            await button.first.wait_for(state="visible", timeout=10000)
        except PlaywrightTimeout:
            logger.info(
                "No Enviar Código button — may not be required on this flow"
            )
            return

        # The Enviar Código click triggers an async POST to Okta's
        # /idp/idx/challenge endpoint which is what actually dispatches the
        # SMS. Cigna's frontend updates the UI (showing the OTP entry
        # screen) the instant the click handler starts — BEFORE the XHR
        # finishes. wait_for_load_state("networkidle") fires too early here.
        # Explicitly wait for the /idp/idx/challenge response so we know
        # the SMS dispatch reached the gateway before allowing the browser
        # to close.
        try:
            async with page.expect_response(
                lambda r: "/idp/idx/challenge" in r.url
                and r.request.method == "POST",
                timeout=15000,
            ) as resp_info:
                await button.first.click()
                logger.info("Clicked Enviar Código (native click)")
            response = await resp_info.value
            body = await response.text()
            # Log the full body — we need to see ALL the remediations in the
            # Okta response, not just the first one. Truncated views have
            # masked the actual next-step requirements.
            logger.info(
                "SMS dispatch response: status=%d body=%s",
                response.status, body,
            )
        except PlaywrightTimeout:
            logger.warning(
                "Did not observe /idp/idx/challenge response within 15s — "
                "SMS may not have been dispatched"
            )

    async def _capture_screen(self, page: Page, filename: str) -> None:
        try:
            await page.screenshot(
                path=f"infrastructure/cookies/{filename}",
                full_page=True,
            )
        except Exception as exc:
            logger.error("Could not save screenshot %s: %s", filename, exc)


# ---- response → domain mapping ----

def _parse_doctors_response(raw: list[dict], specialty: Specialty) -> list[Doctor]:
    """Turn the raw `content[]` from advanced-search into Doctor entities.

    Each entry in `raw` is a practitioner with one or more `addresses[]`
    (clinics). We emit one `Doctor` per (practitioner × clinic) tuple so
    the booking flow can target a specific clinic to call.

    Entries with no addresses are skipped (no clinic to book at).
    """
    doctors: list[Doctor] = []
    for entry in raw:
        practitioner_id = str(entry.get("id") or "").strip()
        name = (entry.get("name") or "").strip()
        for addr in entry.get("addresses") or []:
            clinic_id = str(addr.get("id") or "").strip()
            clinic_org_id = str(addr.get("idProvider") or "").strip()
            clinic_name = (addr.get("provider") or name).strip()
            doctors.append(
                Doctor(
                    clinic_id=clinic_id,
                    practitioner_id=practitioner_id,
                    clinic_org_id=clinic_org_id,
                    name=name or clinic_name,
                    specialty=specialty,
                    clinic_name=clinic_name,
                    address=_address_from_entry(addr),
                    phone=_clean_phone(addr.get("phone1") or ""),
                    distance_meters=_distance_meters(addr.get("distanceToSearchPoint")),
                )
            )
    return doctors


def _address_from_entry(addr: dict) -> Address:
    geo = addr.get("geoPoint") or {}
    lat, lon = geo.get("lat"), geo.get("lon")
    coords = None
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        coords = Coordinates(latitude=float(lat), longitude=float(lon))
    return Address(
        raw=(addr.get("address") or "").strip(),
        city=(addr.get("city") or None),
        postal_code=(addr.get("postcd") or None),
        country="ES",
        coordinates=coords,
    )


def _distance_meters(distance_km: float | int | None) -> int | None:
    """Cigna returns `distanceToSearchPoint` in kilometers as a float."""
    if distance_km is None:
        return None
    try:
        return int(round(float(distance_km) * 1000))
    except (TypeError, ValueError):
        return None


def _clean_phone(raw: str) -> str:
    return re.sub(r"[^\d+]", "", raw or "")


__all__ = ["CignaScraper"]
