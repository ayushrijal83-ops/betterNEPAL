"""Server-side refresh token records.

Refresh tokens are opaque random strings, not JWTs. Two consequences that
matter:

* An access token can never be mistaken for a refresh token, because the
  refresh endpoint looks its argument up in this table and a JWT will simply
  not be found.
* Revocation is real. Logout marks the row revoked, and the next refresh
  attempt fails.

Only the SHA-256 of the token is stored, so a database leak does not hand out
usable credentials. A fast hash is correct here — unlike a password, the token
is 256 bits of CSPRNG output, so there is nothing to brute-force and the lookup
needs to be deterministic.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import BaseModel, UtcDateTime, utcnow
from .user import User


class RefreshToken(BaseModel):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    user: Mapped[User] = relationship()

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= utcnow()

    @property
    def is_usable(self) -> bool:
        return not self.is_revoked and not self.is_expired

    def revoke(self) -> None:
        if self.revoked_at is None:
            self.revoked_at = utcnow()

    def __repr__(self) -> str:
        return f"<RefreshToken user_id={self.user_id} usable={self.is_usable}>"
