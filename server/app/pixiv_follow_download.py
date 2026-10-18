"""Default authored-work downloads for existing and future Pixiv subscriptions."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import update

from .auth import require_admin_cookie
from .database import get_db
from .integrations.credentials import decrypt_credentials, encrypt_credentials
from .models import NetworkSetting, Subscription
from . import queue_control

KEY = 'pixiv_followed_author_download'
router = APIRouter(prefix='/pixiv/follow-download-settings', dependencies=[Depends(require_admin_cookie)])


def default_works(db):
    row = db.get(NetworkSetting, KEY)
    return bool(row and decrypt_credentials(row.value_encrypted).get('enabled'))


class FollowDownloadInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


@router.get('')
def get_policy(db=Depends(get_db)):
    return {'enabled': default_works(db), 'scope': 'subscribed_author_works'}


@router.put('')
def set_policy(value: FollowDownloadInput, db=Depends(get_db)):
    queue_control.lock_control(db)
    previous = default_works(db)
    row = db.get(NetworkSetting, KEY)
    if row is None:
        row = NetworkSetting(key=KEY)
        db.add(row)
    row.value_encrypted = encrypt_credentials({'enabled': value.enabled})
    if value.enabled and not previous:
        # The existing bounded scheduler checks these authors, deduplicates
        # active jobs and respects queue pause/concurrency controls.
        db.execute(update(Subscription).where(Subscription.platform == 'pixiv',
            Subscription.target_type == 'author', Subscription.enabled.is_(True),
            Subscription.download_works.is_(None), Subscription.auto_download.is_(False))
            .values(next_check_at=None))
    db.commit()
    return get_policy(db)
