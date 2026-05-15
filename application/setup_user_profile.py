"""Use case: collect a new user's profile via WhatsApp."""
from __future__ import annotations

from dataclasses import dataclass

from application.ports import BaseMessagingClient
from domain.entities.user import Insurer, InsurerCredentials, User
from domain.repositories.user_repository import UserRepository
from domain.value_objects.address import Address


@dataclass
class ProfileDraft:
    """In-progress user profile collected step-by-step over WhatsApp."""

    phone: str
    name: str | None = None
    home_address: str | None = None
    insurer: Insurer | None = None
    insurer_username: str | None = None
    insurer_password: str | None = None

    @property
    def is_complete(self) -> bool:
        return all(
            (
                self.name,
                self.home_address,
                self.insurer,
                self.insurer_username,
                self.insurer_password,
            )
        )

    def to_user(self) -> User:
        if not self.is_complete:
            raise ValueError("draft incomplete")
        return User(
            phone=self.phone,
            name=self.name,  # type: ignore[arg-type]
            home_address=Address(raw=self.home_address),  # type: ignore[arg-type]
            insurer_credentials=InsurerCredentials(
                insurer=self.insurer,  # type: ignore[arg-type]
                username=self.insurer_username,  # type: ignore[arg-type]
                password=self.insurer_password,  # type: ignore[arg-type]
            ),
        )


class SetupUserProfileUseCase:
    def __init__(self, user_repo: UserRepository, whatsapp: BaseMessagingClient):
        self._user_repo = user_repo
        self._whatsapp = whatsapp

    async def start(self, phone: str) -> None:
        """Kick off the profile-collection flow."""
        await self._whatsapp.send_text(
            phone,
            "Hi! I'll help you book private health appointments. "
            "First, what's your full name?",
        )

    async def save(self, draft: ProfileDraft) -> User:
        user = draft.to_user()
        await self._user_repo.save(user)
        await self._whatsapp.send_text(
            user.phone,
            f"Thanks {user.name.split()[0]}! You're all set. "
            "Send me a message like 'book me a psychologist' to get started.",
        )
        return user
