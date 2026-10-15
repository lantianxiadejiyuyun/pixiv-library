from typing import Annotated, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .auth import require_auth
from .database import get_db
from . import managed_proxy as service
from .proxy_subscription import SubscriptionError

router = APIRouter(dependencies=[Depends(require_auth)])


class SubscriptionInput(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    url: SecretStr = Field(min_length=1, max_length=4096)
    auto_update: bool = True
    interval_hours: int = Field(default=24, ge=1, le=168)
    fetch_via_proxy: bool = False

    @field_validator("label")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("订阅名称不能为空")
        return value.strip()


class SubscriptionPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=100)
    url: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    auto_update: bool | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=168)
    fetch_via_proxy: bool | None = None

    @field_validator("label")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("订阅名称不能为空")
        return value.strip() if value is not None else None


class EnableInput(BaseModel):
    enabled: bool


class NodeInput(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)


class BatchDelayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] | None = Field(default=None, min_length=1, max_length=2000)


def safely(operation: Callable):
    try:
        return operation()
    except SubscriptionError as exc:
        raise HTTPException(400, str(exc)) from None
    except (ValueError, RuntimeError):
        # Core logs/config can contain proxy passwords; never reflect them.
        raise HTTPException(503, "内置代理无法应用配置，请检查内核状态；原订阅与选择保持不变") from None


@router.get("/network/managed")
def managed_status(db=Depends(get_db)):
    return safely(lambda: service.public_state(db))


@router.post("/network/managed/subscriptions")
def add_subscription(value: SubscriptionInput, db=Depends(get_db)):
    data = value.model_dump()
    data["url"] = value.url.get_secret_value().strip()
    return safely(lambda: service.create_subscription(db, data))


@router.patch("/network/managed/subscriptions/{subscription_id}")
def patch_subscription(subscription_id: str, value: SubscriptionPatch, db=Depends(get_db)):
    changes = value.model_dump(exclude_none=True, exclude_unset=True)
    if value.url is not None:
        changes["url"] = value.url.get_secret_value().strip()
    return safely(lambda: service.update_subscription(db, subscription_id, changes))


@router.post("/network/managed/subscriptions/{subscription_id}/refresh")
def refresh_subscription(subscription_id: str, db=Depends(get_db)):
    return safely(lambda: service.update_subscription(db, subscription_id, refresh=True))


@router.delete("/network/managed/subscriptions/{subscription_id}")
def remove_subscription(subscription_id: str, db=Depends(get_db)):
    return safely(lambda: service.delete_subscription(db, subscription_id))


@router.post("/network/managed/enable")
def set_enabled(value: EnableInput, db=Depends(get_db)):
    return safely(lambda: service.enable_proxy(db, value.enabled))


@router.post("/network/managed/select")
def choose_node(value: NodeInput, db=Depends(get_db)):
    return safely(lambda: service.select_node(db, value.node_id))


@router.post("/network/managed/delay")
def node_delay(value: NodeInput, db=Depends(get_db)):
    return safely(lambda: service.measure_delay(db, value.node_id))


@router.post("/network/managed/delay-batch", status_code=202)
def start_delay_batch(value: BatchDelayInput | None = None, db=Depends(get_db)):
    from .managed_proxy_delay import batches
    return safely(lambda: batches.start(db, value.node_ids if value else None))


@router.get("/network/managed/delay-batch")
def latest_delay_batch():
    from .managed_proxy_delay import batches
    return batches.get()


@router.get("/network/managed/delay-batch/{batch_id}")
def delay_batch_status(batch_id: str):
    from .managed_proxy_delay import batches
    result = batches.get(batch_id)
    if result is None:
        raise HTTPException(404, "测速批次不存在或已过期，请重新测速")
    return result


@router.post("/network/managed/restart")
def restart(db=Depends(get_db)):
    return safely(lambda: service.restart_core(db))
