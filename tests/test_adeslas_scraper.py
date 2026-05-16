"""Unit tests for the Adeslas scraper.

Playwright is fully mocked so these run without a real browser.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application.ports import OTPRequired
from domain.entities.user import Insurer, InsurerCredentials, User
from domain.value_objects.address import Address
from domain.value_objects.specialty import Specialty
from domain.value_objects.time_slot import AvailabilityWindow
from infrastructure.scrapers.adeslas_scraper import (
    AdeslasScraper,
    _clean_phone,
    _parse_distance,
)


@pytest.fixture
def scraper(tmp_path: Path) -> AdeslasScraper:
    return AdeslasScraper(cookies_dir=tmp_path)


@pytest.fixture
def sample_user() -> User:
    return User(
        phone="+34600111222",
        name="Test User",
        home_address=Address(raw="Madrid"),
        insurer_credentials=InsurerCredentials(
            insurer=Insurer.ADESLAS,
            username="12345678A",
            password="secret",
        ),
        availability=AvailabilityWindow(),
        created_at=datetime.utcnow(),
    )


def _make_locator_result(**overrides) -> MagicMock:
    """Return a locator result whose `.first` has sensible async defaults."""
    first = MagicMock()
    first.is_visible = AsyncMock(return_value=False)
    first.wait_for = AsyncMock()
    first.fill = AsyncMock()
    first.click = AsyncMock()
    first.check = AsyncMock()
    first.inner_text = AsyncMock(return_value="")
    first.count = AsyncMock(return_value=1)
    for key, val in overrides.items():
        setattr(first, key, val)
    result = MagicMock()
    result.first = first
    return result


@pytest.fixture
def mock_playwright_context():
    """Yield mocks for playwright → browser → context → page."""
    mock_page = MagicMock()
    mock_page.goto = AsyncMock()
    mock_page.locator = MagicMock(return_value=_make_locator_result())
    mock_page.keyboard = MagicMock(press=AsyncMock())

    mock_context = MagicMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.set_default_timeout = MagicMock()
    mock_context.storage_state = AsyncMock()

    mock_browser = MagicMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock()

    mock_pw = MagicMock()
    mock_pw.chromium = MagicMock()
    mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)

    return mock_pw, mock_browser, mock_context, mock_page


# ---------------------------------------------------------------------------
# find_doctors entry-point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_doctors_raises_otp_required(
    scraper: AdeslasScraper,
    sample_user: User,
    mock_playwright_context,
):
    """If an OTP input appears and no otp_code is provided, OTPRequired is raised."""
    mock_pw, _mock_browser, _mock_context, mock_page = mock_playwright_context

    mock_page.url = "https://www.segurcaixaadeslas.es/login"

    async def fake_goto(url, **_):
        mock_page.url = url

    mock_page.goto = fake_goto

    # On the login page the DNI input is visible → _looks_like_login returns True
    nie_input = _make_locator_result(
        wait_for=AsyncMock(), fill=AsyncMock(), is_visible=AsyncMock(return_value=True)
    )
    pw_input = _make_locator_result(fill=AsyncMock())
    otp_input = _make_locator_result(is_visible=AsyncMock(return_value=True))
    login_btn = _make_locator_result(click=AsyncMock())

    def locator_side_effect(selector: str):
        sel = selector.lower()
        if any(k in sel for k in ["dni", "nie", "username"]):
            return nie_input
        if "password" in sel or "contrasea" in sel:
            return pw_input
        if any(k in sel for k in ["otp", "codigo", "cdigo", "verificacin"]):
            return otp_input
        if any(k in sel for k in ["doctor-card", "doctor", "article", "provider-card", "result-item"]):
            cards = MagicMock()
            cards.count = AsyncMock(return_value=0)
            return cards
        return login_btn

    mock_page.locator = locator_side_effect
    mock_page.wait_for_load_state = AsyncMock()
    mock_page.wait_for_selector = AsyncMock()

    with patch(
        "infrastructure.scrapers.adeslas_scraper.async_playwright"
    ) as mock_apw:
        mock_apw.return_value.__aenter__ = AsyncMock(return_value=mock_pw)
        with pytest.raises(OTPRequired):
            await scraper.find_doctors(
                user=sample_user,
                specialty=Specialty.PSICOLOGIA,
                near=Address(raw="Madrid"),
            )


@pytest.mark.asyncio
async def test_find_doctors_returns_parsed_doctors(
    scraper: AdeslasScraper,
    sample_user: User,
    mock_playwright_context,
):
    """Happy path: logged-in user, search returns two doctor cards."""
    mock_pw, _mock_browser, _mock_context, mock_page = mock_playwright_context

    async def fake_goto(url, **_):
        mock_page.url = url

    mock_page.goto = fake_goto

    def fake_locator(selector: str):
        sel = selector.lower()
        if any(k in sel for k in ["lugar", "dirección", "provincia", "location"]):
            return _make_locator_result(wait_for=AsyncMock(), fill=AsyncMock())
        if any(k in sel for k in ["especialidad", "specialty"]):
            return _make_locator_result(fill=AsyncMock())
        if any(k in sel for k in ["buscar", "consultar", "submit"]):
            return _make_locator_result(click=AsyncMock())
        return _make_locator_result()

    mock_page.locator = fake_locator
    mock_page.wait_for_selector = AsyncMock()
    mock_page.keyboard.press = AsyncMock()

    # ---- Mock two doctor cards ----
    card0 = MagicMock()
    card1 = MagicMock()

    def make_card_locator(card_name: str):
        def card_locator_side_effect(selector: str):
            sel = selector.lower()
            if any(k in sel for k in ["name", "h2", "h3"]):
                return _make_locator_result(
                    inner_text=AsyncMock(
                        return_value="Dr. Ana" if card_name == "card0" else "Dr. Luis"
                    )
                )
            if any(k in sel for k in ["address", "location"]):
                return _make_locator_result(
                    inner_text=AsyncMock(
                        return_value="Calle Mayor 1" if card_name == "card0" else "Calle Sol 2"
                    )
                )
            if any(k in sel for k in ["phone", "tel:", "telefono"]):
                return _make_locator_result(
                    inner_text=AsyncMock(
                        return_value="+34 600 111 222" if card_name == "card0" else "+34 600 333 444"
                    )
                )
            if any(k in sel for k in ["distance", "proximity"]):
                return _make_locator_result(
                    inner_text=AsyncMock(
                        return_value="450 m" if card_name == "card0" else "1,2 km"
                    )
                )
            if any(k in sel for k in ["clinic", "center", "centro", "provider"]):
                return _make_locator_result(
                    inner_text=AsyncMock(
                        return_value="Clinica Norte" if card_name == "card0" else "Clinica Sur"
                    )
                )
            return _make_locator_result()

        return card_locator_side_effect

    card0.locator = make_card_locator("card0")
    card1.locator = make_card_locator("card1")

    cards_collection = MagicMock()
    cards_collection.count = AsyncMock(return_value=2)
    cards_collection.nth = lambda i: card0 if i == 0 else card1

    original_locator = mock_page.locator

    def page_locator(selector: str):
        sel = selector.lower()
        if any(k in sel for k in ["doctor-card", "doctor", "article", "provider-card", "result-item"]):
            return cards_collection
        return original_locator(selector)

    mock_page.locator = page_locator

    with patch(
        "infrastructure.scrapers.adeslas_scraper.async_playwright"
    ) as mock_apw:
        mock_apw.return_value.__aenter__ = AsyncMock(return_value=mock_pw)
        doctors = await scraper.find_doctors(
            user=sample_user,
            specialty=Specialty.PSICOLOGIA,
            near=Address(raw="Madrid"),
        )

    assert len(doctors) == 2
    assert doctors[0].name == "Dr. Ana"
    assert doctors[0].distance_meters == 450
    assert doctors[1].name == "Dr. Luis"
    assert doctors[1].distance_meters == 1200


@pytest.mark.asyncio
async def test_cookie_reuse_skips_login(
    scraper: AdeslasScraper,
    sample_user: User,
    mock_playwright_context,
):
    """If a cookie file exists, _ensure_logged_in should skip the login form."""
    mock_pw, _mock_browser, _mock_context, mock_page = mock_playwright_context

    cookie_path = scraper._cookie_path(sample_user.phone)
    cookie_path.write_text('{"cookies": []}', encoding="utf-8")

    async def fake_goto(url, **_):
        mock_page.url = url

    mock_page.goto = fake_goto

    def fake_locator(selector: str):
        sel = selector.lower()
        if any(k in sel for k in ["dni", "nie", "username"]):
            return _make_locator_result(is_visible=AsyncMock(return_value=False))
        if any(k in sel for k in ["lugar", "dirección", "provincia"]):
            return _make_locator_result(wait_for=AsyncMock(), fill=AsyncMock())
        if "especialidad" in sel:
            return _make_locator_result(fill=AsyncMock())
        if "buscar" in sel:
            return _make_locator_result(click=AsyncMock())
        return _make_locator_result()

    mock_page.locator = fake_locator
    mock_page.wait_for_selector = AsyncMock()
    mock_page.keyboard.press = AsyncMock()

    cards_collection = MagicMock()
    cards_collection.count = AsyncMock(return_value=0)

    original_locator = mock_page.locator

    def page_locator(selector: str):
        sel = selector.lower()
        if any(k in sel for k in ["doctor-card", "doctor", "article", "provider-card", "result-item"]):
            return cards_collection
        return original_locator(selector)

    mock_page.locator = page_locator

    with patch(
        "infrastructure.scrapers.adeslas_scraper.async_playwright"
    ) as mock_apw:
        mock_apw.return_value.__aenter__ = AsyncMock(return_value=mock_pw)
        doctors = await scraper.find_doctors(
            user=sample_user,
            specialty=Specialty.PSICOLOGIA,
            near=Address(raw="Madrid"),
        )

    assert doctors == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_cookie_path(scraper: AdeslasScraper):
    """_cookie_path sanitises the phone and uses the correct prefix."""
    path = scraper._cookie_path("+34 600 111 222")
    assert path.name == "adeslas__34_600_111_222.json"
    assert "adeslas" in path.name


def test_parse_distance():
    """_parse_distance handles metres and kilometres with comma/point decimals."""
    assert _parse_distance("450 m") == 450
    assert _parse_distance("1,2 km") == 1200
    assert _parse_distance("1.5 km") == 1500
    assert _parse_distance("") is None
    assert _parse_distance("foo") is None


def test_clean_phone():
    """_clean_phone strips everything except digits and the plus sign."""
    assert _clean_phone("+34 600 111 222") == "+34600111222"
    assert _clean_phone("91 123 45 67") == "911234567"
    assert _clean_phone("") == ""
