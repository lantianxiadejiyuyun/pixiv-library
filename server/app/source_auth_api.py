"""Web-facing source login endpoints, separate from advanced credential import."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr, field_validator

from .auth import COOKIE_NAME, require_auth
from .database import get_db
from .integrations.base import ConnectorError
from .integrations.credentials import encrypt_credentials
from .models import SourceAccount, utcnow
from .source_auth import SourceLoginError, login_platform

router = APIRouter(dependencies=[Depends(require_auth)])


class PlatformLogin(BaseModel):
    label: str = Field(min_length=1, max_length=191)
    username: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=2048)

    @field_validator("label", "username")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("名称和账号不能为空")
        return value.strip()


class OAuthFinish(BaseModel):
    session_id: str = Field(min_length=1, max_length=256)
    code: SecretStr = Field(min_length=1, max_length=8192)
    label: str = Field(min_length=1, max_length=191)

    @field_validator("label")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("账号名称不能为空")
        return value.strip()


def _save_verified(db, platform, label, credentials):
    account = SourceAccount(platform=platform, label=label,
        credentials_encrypted=encrypt_credentials(credentials), verified_at=utcnow())
    db.add(account)
    db.commit()
    db.refresh(account)
    return {"id": account.id, "label": account.label, "platform": account.platform,
            "auth_status": "verified", "verified_at": account.verified_at.isoformat() + "Z"}


@router.post("/sources/{platform}/login")
def platform_login(platform: str, value: PlatformLogin, db=Depends(get_db)):
    try:
        credentials = login_platform(platform, value.username, value.password.get_secret_value())
    except SourceLoginError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _save_verified(db, platform, value.label, credentials)


@router.post("/sources/pixiv/oauth/start")
def pixiv_oauth_start(request: Request):
    from .integrations.pixiv_auth import OAuthRateLimitError, start_oauth
    try:
        return start_oauth(request.cookies[COOKIE_NAME])
    except OAuthRateLimitError as exc:
        raise HTTPException(429, str(exc)) from exc
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sources/pixiv/oauth/finish")
def pixiv_oauth_finish(value: OAuthFinish, request: Request, db=Depends(get_db)):
    from .integrations.pixiv_auth import OAuthRateLimitError, finish_oauth
    try:
        credentials = finish_oauth(request.cookies[COOKIE_NAME], value.session_id, value.code.get_secret_value())
    except OAuthRateLimitError as exc:
        raise HTTPException(429, str(exc)) from exc
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _save_verified(db, "pixiv", value.label, credentials)
