"""Adeslas (SegurCaixa Adeslas) cuadro médico scraper.

Flow:
  1. Try loading cached cookies for the user. If valid → navigate straight
     to /cuadromedico.
  2. Otherwise log in with user.insurer_credentials (DNI/NIE + password).
     If a verification screen appears, raise OTPRequired so the orchestrator
     can solicit a code via WhatsApp and retry with otp_code set.
  3. On the cuadro médico page: fill location + specialty, submit search,
     parse doctor cards, return sorted by proximity.

Portal under test: https://www.segurcaixaadeslas.es/cuadromedico
Requires: playwright install chromium
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Page,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeout,
)

from application.ports import BaseInsurerScraper, OTPRequired
from domain.entities.doctor import Doctor
from domain.entities.user import User
from domain.value_objects.address import Address
from domain.value_objects.specialty import Specialty

logger = logging.getLogger(__name__)

_DEFAULT_LOGIN_URL = "https://www.segurcaixaadeslas.es"
_DEFAULT_DOCTORS_URL = "https://www.segurcaixaadeslas.es/cuadromedico"


class AdeslasScraper(BaseInsurerScraper):
    """Playwright-based scraper for the Adeslas (SegurCaixa) insurer portal.

    Implements BaseInsurerScraper.find_doctors() to search the cuadro médico
    for professionals by specialty and location.

    Constructor args:
        cookies_dir: Path where per-user session cookies are persisted.
        login_url:   Adeslas login / homepage URL.
        doctors_url: Cuadro médico search page URL.
        headless:    Run browser in headless mode (default True).
        timeout_ms:  Default Playwright action timeout in milliseconds.
    """

    def __init__(
        self,
        cookies_dir: Path,
        login_url: str = _DEFAULT_LOGIN_URL,
        doctors_url: str = _DEFAULT_DOCTORS_URL,
        headless: bool = True,
        timeout_ms: int = 30000,
    ):
        self._cookies_dir = Path(cookies_dir)
        self._cookies_dir.mkdir(parents=True, exist_ok=True)
        self._login_url = login_url
        self._doctors_url = doctors_url
        self._headless = headless
        self._timeout_ms = timeout_ms

    # ---- public API ----

    async def find_doctors(
        self,
        user: User,
        specialty: Specialty,
        near: Address,
        otp_code: str | None = None,
    ) -> list[Doctor]:
        """Search Adeslas cuadro médico for doctors near `near` of the given specialty.

        Args:
            user:      Authenticated User with Adeslas insurer credentials.
            specialty: Medical specialty to search for (e.g. PSICOLOGIA).
            near:      Location to search near (raw address string).
            otp_code:  SMS/email verification code, if an OTP challenge was
                       raised on a previous call. None on first attempt.

        Returns:
            List of Doctor entities sorted by proximity.

        Raises:
            OTPRequired: If the login flow presents an OTP/verification screen
                         and no otp_code was supplied.
        """
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
                return await self._search_doctors(page, specialty, near)
            finally:
                await browser.close()

    # ---- login / session ----

    def _cookie_path(self, phone: str) -> Path:
        """Return the per-user cookie file path.

        Args:
            phone: E.164 phone number (e.g. '+34612345678').

        Returns:
            Path like cookies_dir/adeslas_{sanitized_phone}.json
        """
        safe = re.sub(r"[^0-9A-Za-z]", "_", phone)
        return self._cookies_dir / f"adeslas_{safe}.json"

    async def _ensure_logged_in(
        self,
        page: Page,
        context: BrowserContext,
        user: User,
        otp_code: str | None,
    ) -> None:
        """Navigate to cuadro médico; log in if the session is stale.

        Args:
            page:     Playwright Page in a fresh BrowserContext.
            context:  BrowserContext (to persist cookies post-login).
            user:     User with Adeslas credentials.
            otp_code: Optional OTP code for 2FA challenge.

        Raises:
            OTPRequired: If an OTP screen appears and no code was supplied.
        """
        await page.goto(self._doctors_url, wait_until="domcontentloaded")
        if await self._is_authenticated(page):
            logger.info("Adeslas session resumed from cached cookies for %s", user.phone)
            return

        logger.info("No valid session; logging in fresh for %s", user.phone)
        await page.goto(self._login_url, wait_until="domcontentloaded")
        await self._dismiss_cookies_banner(page)
        await self._fill_login_form(page, user)
        await self._maybe_handle_otp(page, otp_code)

        await page.goto(self._doctors_url, wait_until="domcontentloaded")
        if not await self._is_authenticated(page):
            raise RuntimeError(
                "Adeslas login appeared to succeed but cuadro médico is not accessible"
            )
        await context.storage_state(path=str(self._cookie_path(user.phone)))
        logger.info("Saved Adeslas cookies for %s", user.phone)

    async def _is_authenticated(self, page: Page) -> bool:
        """Heuristic: cuadro médico page renders only when logged in."""
        try:
            return "/cuadromedico" in page.url and not await self._looks_like_login(page)
        except Exception:
            return False

    async def _looks_like_login(self, page: Page) -> bool:
        login_input = page.locator(
            'input[placeholder*="DNI"], input[placeholder*="NIE"], '
            'input[name*="dni" i], input[name*="nie" i], '
            'input[name*="username" i]'
        ).first
        try:
            return await login_input.is_visible(timeout=1500)
        except PlaywrightTimeout:
            return False

    async def _dismiss_cookies_banner(self, page: Page) -> None:
        for selector in (
            'button:has-text("Aceptar")',
            'button:has-text("Aceptar todo")',
            'button:has-text("Permitir todas")',
            '#onetrust-accept-btn-handler',
        ):
            btn = page.locator(selector).first
            try:
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    return
            except PlaywrightTimeout:
                continue

    async def _fill_login_form(self, page: Page, user: User) -> None:
        nie_input = page.locator(
            'input[placeholder*="DNI"], input[placeholder*="NIE"], '
            'input[name*="dni" i], input[name*="nie" i]'
        ).first
        pw_input = page.locator(
            'input[type="password"], input[placeholder*="contraseña" i]'
        ).first
        await nie_input.wait_for(state="visible")
        await nie_input.fill(user.insurer_credentials.username)
        await pw_input.fill(user.insurer_credentials.password)
        await page.locator(
            'button:has-text("Acceder"), button:has-text("Entrar"), '
            'button[type="submit"]'
        ).first.click()

    async def _maybe_handle_otp(self, page: Page, otp_code: str | None) -> None:
        """If Adeslas asks for a verification code, type it or raise OTPRequired."""
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeout:
            pass

        otp_input = page.locator(
            'input[autocomplete="one-time-code"], '
            'input[name*="otp" i], '
            'input[name*="codigo" i], '
            'input[placeholder*="código" i], '
            'input[placeholder*="verificación" i]'
        ).first

        try:
            visible = await otp_input.is_visible(timeout=2500)
        except PlaywrightTimeout:
            visible = False

        if not visible:
            return  # no OTP screen — login proceeded directly

        if not otp_code:
            logger.info("Adeslas OTP screen detected; signalling OTPRequired")
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

    # ---- search ----

    async def _search_doctors(
        self, page: Page, specialty: Specialty, near: Address
    ) -> list[Doctor]:
        await page.goto(self._doctors_url, wait_until="domcontentloaded")
        await self._dismiss_cookies_banner(page)

        # 1. Fill location.
        loc_input = page.locator(
            'input[placeholder*="lugar" i], '
            'input[placeholder*="dirección" i], '
            'input[placeholder*="provincia" i], '
            'input[name*="location" i]'
        ).first
        await loc_input.wait_for(state="visible")
        await loc_input.fill(near.raw)
        await self._pick_first_autocomplete(page)

        # 2. Fill specialty.
        sp_input = page.locator(
            'input[placeholder*="especialidad" i], '
            'select[name*="specialty" i], '
            'input[name*="especialidad" i]'
        ).first
        await sp_input.fill(specialty.value)
        await self._pick_autocomplete_match(page, specialty.value)

        # 3. Submit.
        await page.locator(
            'button:has-text("Buscar"), '
            'button:has-text("Consultar"), '
            'input[type="submit"]'
        ).first.click()
        await page.wait_for_selector(
            '.doctor-card, [data-testid*="doctor"], article, '
            '.provider-card, .result-item',
            timeout=self._timeout_ms,
        )

        # 4. Sort by proximity if available.
        await self._sort_by_proximity(page)

        return await self._parse_doctor_cards(page, specialty)

    async def _pick_first_autocomplete(self, page: Page) -> None:
        try:
            await page.wait_for_selector(
                '.autocomplete-dropdown, [role="listbox"], .dropdown-menu',
                timeout=3000
            )
            await page.locator(
                '.autocomplete-option, [role="option"], .dropdown-item'
            ).first.click()
        except PlaywrightTimeout:
            pass

    async def _pick_autocomplete_match(self, page: Page, text: str) -> None:
        for selector in (
            f'.autocomplete-option:has-text("{text}")',
            f'[role="option"]:has-text("{text}")',
            f'.dropdown-item:has-text("{text}")',
            f'li:has-text("{text}")',
        ):
            try:
                opt = page.locator(selector).first
                if await opt.is_visible(timeout=2500):
                    await opt.click()
                    return
            except PlaywrightTimeout:
                continue
        await page.keyboard.press("Enter")

    async def _sort_by_proximity(self, page: Page) -> None:
        for selector in (
            'select:has(option:has-text("Cercanía"))',
            'select:has(option:has-text("Proximidad"))',
            '[aria-label*="Ordenar" i]',
        ):
            try:
                dropdown = page.locator(selector).first
                if await dropdown.is_visible(timeout=1500):
                    await dropdown.click()
                    cerc = page.locator('text=/Cercanía/i').first
                    if await cerc.is_visible(timeout=1500):
                        await cerc.click()
                    return
            except PlaywrightTimeout:
                continue

    async def _parse_doctor_cards(
        self, page: Page, specialty: Specialty
    ) -> list[Doctor]:
        cards = page.locator(
            '.doctor-card, [data-testid*="doctor"], article, '
            '.provider-card, .result-item'
        )
        count = await cards.count()
        doctors: list[Doctor] = []
        for i in range(count):
            card = cards.nth(i)
            name = await _safe_text(card, '.doctor-name, h2, h3, .name')
            address = await _safe_text(
                card, '.address, [data-testid*="address"], .location'
            )
            phone = await _safe_text(
                card, '.phone, a[href^="tel:"], .telefono'
            )
            distance = _parse_distance(
                await _safe_text(
                    card, '.distance, [data-testid*="distance"], .proximity'
                )
            )
            clinic = await _safe_text(
                card, '.clinic-name, .center, .centro, .provider-name'
            ) or name
            if not name and not phone:
                continue
            doctors.append(
                Doctor(
                    id=f"adeslas:{name}:{phone}".strip(),
                    name=name or clinic,
                    specialty=specialty,
                    clinic_name=clinic,
                    address=Address(raw=address or ""),
                    phone=_clean_phone(phone),
                    distance_meters=distance,
                )
            )
        logger.info(
            "Parsed %d Adeslas doctor cards for %s", len(doctors), specialty.value
        )
        return doctors


# ---- helpers ----

async def _safe_text(card, selector: str) -> str:
    try:
        el = card.locator(selector).first
        if await el.count() == 0:
            return ""
        text = await el.inner_text(timeout=2000)
        return text.strip()
    except PlaywrightTimeout:
        return ""
    except Exception:
        return ""


def _parse_distance(text: str) -> int | None:
    """Extract distance in meters from strings like '450 m' or '1,2 km'."""
    if not text:
        return None
    m = re.search(r"([\d.,]+)\s*(km|m)", text, flags=re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    return int(value * 1000) if unit == "km" else int(value)


def _clean_phone(raw: str) -> str:
    digits = re.sub(r"[^\d+]", "", raw or "")
    return digits


__all__ = ["AdeslasScraper"]


# Optional: dump scraped JSON for debugging.
def doctors_to_dict(doctors: list[Doctor]) -> list[dict]:
    return [
        {
            "id": d.id,
            "name": d.name,
            "specialty": d.specialty.value,
            "clinic_name": d.clinic_name,
            "address": d.address.raw,
            "phone": d.phone,
            "distance_meters": d.distance_meters,
        }
        for d in doctors
    ]


def dump_doctors_json(doctors: list[Doctor]) -> str:
    return json.dumps(doctors_to_dict(doctors), ensure_ascii=False, indent=2)
