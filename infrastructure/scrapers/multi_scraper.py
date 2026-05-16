"""Router scraper that dispatches to the correct insurer implementation."""
from __future__ import annotations

from application.ports import BaseInsurerScraper
from domain.entities.doctor import Doctor
from domain.entities.user import Insurer, User
from domain.value_objects.address import Address
from domain.value_objects.specialty import Specialty


class MultiInsurerScraper(BaseInsurerScraper):
    """Wraps multiple concrete scrapers and routes by user.insurer.

    This keeps BookAppointmentUseCase agnostic — it still sees a single
    BaseInsurerScraper port, while behind the scenes we choose Cigna,
    Adeslas, etc. at runtime.

    Constructor args:
        scrapers: Mapping from Insurer enum to concrete scraper instance.
    """

    def __init__(self, scrapers: dict[Insurer, BaseInsurerScraper]) -> None:
        self._scrapers = scrapers

    async def find_doctors(
        self,
        user: User,
        specialty: Specialty,
        near: Address,
        otp_code: str | None = None,
    ) -> list[Doctor]:
        """Dispatch to the concrete scraper for the user's insurer.

        Args:
            user:      User entity whose insurer_credentials.insurer selects
                       the concrete scraper.
            specialty: Medical specialty to search.
            near:      Address / location to search near.
            otp_code:  Optional OTP verification code.

        Returns:
            List of Doctor entities from the insurer's portal.

        Raises:
            ValueError: If no scraper is registered for the user's insurer.
            OTPRequired: Delegated from the concrete scraper if OTP is needed.
        """
        insurer = user.insurer
        scraper = self._scrapers.get(insurer)
        if scraper is None:
            raise ValueError(f"No scraper configured for insurer: {insurer.value}")
        return await scraper.find_doctors(user, specialty, near, otp_code)


__all__ = ["MultiInsurerScraper"]
