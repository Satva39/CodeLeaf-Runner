from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from .models import OWNER_ID_RE

from .config import get_settings


def valid_service_key(provided: str, expected: str) -> bool:
    return len(expected) >= 32 and bool(provided) and hmac.compare_digest(provided, expected)


def require_runner_key(
    x_runner_key: str = Header(default="", alias="X-Runner-Key"),
) -> None:
    expected = get_settings().token_value
    if not valid_service_key(x_runner_key, expected):
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
