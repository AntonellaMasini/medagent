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

import logging
import re
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from application.ports import BaseInsurerScraper, OTPRequired
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
    ) -> list[Doctor]:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self._headless)
            try:
                context = await browser.new_context(
                    storage_state=self._cookie_path(user.phone)
                    if self._cookie_path(user.phone).exists()
                    else None
                )
                context.set_default_timeout(self._timeout_ms)
                page = await context.new_page()

                await self._ensure_logged_in(page, context, user, otp_code)

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
    ) -> None:
        await page.goto(self._doctors_url, wait_until="domcontentloaded")
        if await self._is_authenticated(page):
            logger.info("Cigna session resumed from cached cookies for %s", user.phone)
            return

        logger.info("No valid session; logging in fresh for %s", user.phone)
        await page.goto(self._login_url, wait_until="domcontentloaded")
        await self._dismiss_cookies_banner(page)
        await self._fill_login_form(page, user)
        await self._maybe_handle_otp(page, otp_code)

        await page.goto(self._doctors_url, wait_until="domcontentloaded")
        if not await self._is_authenticated(page):
            raise RuntimeError(
                "Cigna login appeared to succeed but cuadro médico is not accessible"
            )
        await context.storage_state(path=str(self._cookie_path(user.phone)))
        logger.info("Saved Cigna cookies for %s", user.phone)

    async def _is_authenticated(self, page: Page) -> bool:
        try:
            return "/cp/cuadro-medico" in page.url and not await self._looks_like_login(
                page
            )
        except Exception:
            return False

    async def _looks_like_login(self, page: Page) -> bool:
        login_input = page.locator('input[placeholder*="NIE"]').first
        try:
            return await login_input.is_visible(timeout=1500)
        except PlaywrightTimeout:
            return False

    async def _dismiss_cookies_banner(self, page: Page) -> None:
        for selector in (
            'button:has-text("Aceptar")',
            'button:has-text("Aceptar todo")',
            "#onetrust-accept-btn-handler",
        ):
            btn = page.locator(selector).first
            try:
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    return
            except PlaywrightTimeout:
                continue

    async def _fill_login_form(self, page: Page, user: User) -> None:
        nie_input = page.locator('input[placeholder*="NIE"]').first
        pw_input = page.locator('input[placeholder*="Contraseña"]').first
        await nie_input.wait_for(state="visible")
        await nie_input.fill(user.insurer_credentials.username)
        await pw_input.fill(user.insurer_credentials.password)
        await page.locator('button:has-text("Acceder")').first.click()

    async def _maybe_handle_otp(self, page: Page, otp_code: str | None) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeout:
            pass

        otp_input = page.locator(
            'input[autocomplete="one-time-code"], '
            'input[name*="otp" i], '
            'input[name*="codigo" i], '
            'input[placeholder*="código" i]'
        ).first

        try:
            visible = await otp_input.is_visible(timeout=2500)
        except PlaywrightTimeout:
            visible = False

        if not visible:
            return  # no OTP screen — login proceeded directly

        if not otp_code:
            logger.info("Cigna OTP screen detected; signalling OTPRequired")
            raise OTPRequired()

        await otp_input.fill(otp_code)
        for label in ("Verificar", "Continuar", "Validar", "Enviar"):
            btn = page.locator(f'button:has-text("{label}")').first
            try:
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    break
            except PlaywrightTimeout:
                continue
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeout:
            pass


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
            clinic_name = (addr.get("provider") or name).strip()
            doctors.append(
                Doctor(
                    clinic_id=clinic_id,
                    practitioner_id=practitioner_id,
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
