"""Validated configuration inputs and server-only credential encryption."""
import os
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


class ServicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    base_url: str = ""
    model: str = ""
    temperature: float = Field(default=0.2, ge=0, le=2)
    request_timeout_seconds: StrictInt = Field(default=60, gt=0)
    multimodal_mode: Literal["simulation"] = "simulation"
    api_key: str = ""

    @field_validator("base_url", "model", "api_key")
    @classmethod
    def validate_text(cls, value, info):
        value = value.strip()
        if info.field_name == "base_url" and value:
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("服务地址须为不含凭据、查询参数的 HTTP(S) 地址")
        if info.field_name == "api_key" and (not value or "•" in value or "*" in value):
            raise ValueError("请输入新的有效密钥，不能提交空值或掩码")
        return value


class QueuePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    concurrency: StrictInt = Field(default=2, gt=0)
    execution_timeout_seconds: StrictInt = Field(default=60, gt=0)
    max_auto_retries: StrictInt = Field(default=2, ge=0)
    poll_interval_seconds: StrictInt = Field(default=2, gt=0)
    dispatch_interval_seconds: StrictInt = Field(default=1, gt=0)
    heartbeat_interval_seconds: StrictInt = Field(default=10, gt=0)


class AuthorizationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_status: Literal["not_configured", "authorized", "expired"]


def cipher() -> Fernet:
    try:
        return Fernet(os.environ.get("CONFIG_ENCRYPTION_KEY", "").encode("ascii"))
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(503, detail={"code": "encryption_key_invalid", "message": "后端 CONFIG_ENCRYPTION_KEY 缺失或无效，无法保存或使用模型密钥"}) from None


def encrypt_key(value: str) -> str:
    return cipher().encrypt(value.encode()).decode("ascii")


def decrypt_key(value: str) -> str:
    try:
        return cipher().decrypt(value.encode("ascii")).decode()
    except (InvalidToken, UnicodeError):
        raise HTTPException(503, detail={"code": "credential_unreadable", "message": "模型密钥无法解密，请检查后端加密配置"}) from None


def public_services(item: dict) -> dict:
    configured = bool(item.pop("api_key_ciphertext", None) or item.get("api_key"))
    item.pop("api_key", None)
    return {**item, "api_key_configured": configured, "api_key_masked": "••••••••" if configured else ""}


def public_queue(item: dict) -> dict:
    item.pop("heartbeat_seconds", None)
    return item
