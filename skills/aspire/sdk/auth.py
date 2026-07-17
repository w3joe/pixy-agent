"""Aspire API authentication (client credentials)."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class TokenState:
    access_token: str
    expires_at: float  # monotonic deadline

    def is_valid(self, skew_seconds: float = 30.0) -> bool:
        return bool(self.access_token) and time.monotonic() < (self.expires_at - skew_seconds)
