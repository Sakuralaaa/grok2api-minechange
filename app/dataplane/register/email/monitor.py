"""Email provider protocol and registry for registration verification."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EmailProvider(Protocol):
    """Protocol for email providers used during registration."""

    async def create_email(self, domain: str | None = None) -> str:
        """Create a new temporary email address.

        Returns the email address string.
        """
        ...

    async def wait_for_verification_link(
        self,
        email: str,
        sender_pattern: str = "noreply@x.ai",
        timeout: float = 120.0,
        interval: float = 2.0,
    ) -> str | None:
        """Poll the inbox for a verification email and extract the verification link.

        Returns the first verification URL found, or None on timeout.
        """
        ...

    async def dispose_email(self, email: str) -> None:
        """Release / delete the temporary email address."""
        ...

    async def check_connectivity(self) -> dict[str, Any]:
        """Check whether the provider is reachable and operational.

        Returns a dict with keys: `ok` (bool), `message` (str).
        """
        ...
