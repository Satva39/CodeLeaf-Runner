from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from .models import OWNER_ID_RE

from .config import get_settings


def valid_service_key(provided: str, expected: str) -> bool:
    return len(expected) >= 32 and bool(provided) and hmac.compare_digest(provided, expected)


def valid_any_service_key(provided: str, settings=None) -> bool:
    settings = settings or get_settings()
    matched = False
    # Evaluate all configured keys instead of returning early, preserving a
    # constant-time comparison for each candidate during planned token rotation.
    for expected in settings.service_tokens:
        matched = hmac.compare_digest(provided or "", expected) or matched
    return bool(provided) and settings.service_token_ready and matched


def require_runner_key(
    x_runner_key: str = Header(default="", alias="X-Runner-Key"),
) -> None:
    settings = get_settings()
    if not valid_any_service_key(x_runner_key, settings):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runner service authentication failed.",
        )


def require_owner(
    x_codeleaf_user: str = Header(default="", alias="X-CodeLeaf-User"),
) -> str:
    if not x_codeleaf_user or not OWNER_ID_RE.fullmatch(x_codeleaf_user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="CodeLeaf user scope is required.",
        )
    return x_codeleaf_user
