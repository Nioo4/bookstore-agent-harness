from __future__ import annotations

from pathlib import Path

import jwt
from pydantic import BaseModel, ConfigDict, Field


class AuthUser(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: int = Field(gt=0)
    email: str
    email_verified: bool


class AccessTokenVerifier:
    def __init__(self, public_key_path: str, issuer: str) -> None:
        self.public_key = Path(public_key_path).read_text(encoding="utf-8")
        self.issuer = issuer

    def verify_authorization(self, authorization: str | None) -> tuple[str | None, AuthUser | None]:
        if authorization is None or not authorization.strip():
            return None, None
        scheme, separator, token = authorization.strip().partition(" ")
        if separator != " " or scheme.casefold() != "bearer" or not token:
            raise ValueError("invalid authorization header")
        try:
            claims = jwt.decode(
                token,
                self.public_key,
                algorithms=["EdDSA"],
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
            user_id = int(claims["sub"])
            if user_id <= 0:
                raise ValueError("invalid subject")
            return token, AuthUser(
                user_id=user_id,
                email=str(claims.get("email", "")),
                email_verified=bool(claims.get("email_verified", False)),
            )
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid access token") from exc
