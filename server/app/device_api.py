from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update

from .auth import COOKIE_NAME, require_admin_cookie
from .database import get_db
from .device_auth import authenticate_device, create_pairing, exchange_pairing, limit_attempts, normalize_server_url
from .models import DeviceSession, utcnow

router = APIRouter()


class PairingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_url: str = Field(min_length=1, max_length=2048)

    @field_validator("server_url")
    @classmethod
    def base_url(cls, value):
        return normalize_server_url(value)


class ExchangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pairing_token: str = Field(min_length=1, max_length=256)
    device_name: str = Field(min_length=1, max_length=191)

    @field_validator("device_name")
    @classmethod
    def name(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("设备名称不能为空或包含控制字符")
        return value


@router.post("/devices/pairing", dependencies=[Depends(require_admin_cookie)])
def pairing(value: PairingInput, request: Request, db=Depends(get_db)):
    limit_attempts("create", request.cookies.get(COOKIE_NAME, ""), 10, global_limit=30)
    return create_pairing(db, value.server_url)


@router.post("/devices/pairing/exchange")
def exchange(value: ExchangeInput, request: Request, db=Depends(get_db)):
    limit_attempts("exchange", request.client.host if request.client else "unknown", 20, global_limit=120)
    return exchange_pairing(db, value.pairing_token, value.device_name)


@router.get("/devices", dependencies=[Depends(require_admin_cookie)])
def devices(db=Depends(get_db)):
    now = utcnow()
    def iso(value):
        return value.isoformat() + "Z" if value else None
    return {"items": [{"id": device.id, "device_name": device.device_name, "created_at": iso(device.created_at),
        "last_used_at": iso(device.last_used_at), "expires_at": iso(device.expires_at), "revoked_at": iso(device.revoked_at),
        "status": "revoked" if device.revoked_at else "expired" if device.expires_at <= now else "active"}
        for device in db.scalars(select(DeviceSession).order_by(DeviceSession.created_at.desc()).limit(500))]}


@router.delete("/devices/{device_id}", dependencies=[Depends(require_admin_cookie)])
def revoke(device_id: str, db=Depends(get_db)):
    device = db.get(DeviceSession, device_id)
    if device is None:
        raise HTTPException(404, "设备不存在")
    db.execute(update(DeviceSession).where(DeviceSession.id == device_id, DeviceSession.revoked_at.is_(None)).values(revoked_at=utcnow()))
    db.commit()
    return {"ok": True}


@router.post("/devices/disconnect")
def disconnect(request: Request, db=Depends(get_db)):
    device_id = authenticate_device(request)
    db.execute(update(DeviceSession).where(DeviceSession.id == device_id, DeviceSession.revoked_at.is_(None)).values(revoked_at=utcnow()))
    db.commit()
    return {"ok": True}
