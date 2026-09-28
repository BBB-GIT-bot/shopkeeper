from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from typing import Any

from fastapi import Depends, Header, HTTPException, status

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

from .db import connect

SECRET = os.getenv("JWT_SECRET", "shopkeeper-local-secret")
EXPIRES = int(os.getenv("JWT_EXPIRES_SECONDS", "28800"))
password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    if encoded.startswith("$argon2"):
        try: return password_hasher.verify(encoded, password)
        except Argon2Error: return False
    try:
        _, rounds, salt_hex, digest_hex = encoded.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_token(user: dict[str, Any]) -> str:
    return jwt.encode({"sub":str(user["id"]),"exp":int(time.time())+EXPIRES},SECRET,algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token,SECRET,algorithms=["HS256"],options={"require":["sub","exp"]})
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail={"code":"invalid_token","message":"登录已失效，请重新登录"})


def current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "需要登录"})
    body = decode_token(authorization.split(" ", 1)[1])
    with connect() as connection:
        user = connection.execute("SELECT id, username, role, enabled FROM users WHERE id=?", (body["sub"],)).fetchone()
    if not user or not user["enabled"]:
        raise HTTPException(status_code=401, detail={"code": "user_disabled", "message": "账号不可用"})
    return dict(user)


def require_role(*roles: str):
    def dependency(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "当前角色无权执行此操作"})
        return user

    return dependency

