"""Main-identity bookmarks: bounded page jobs and explicit phone credential import."""
from datetime import datetime, timedelta
import hashlib
import json
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .auth import check_password, require_admin_cookie, require_auth
from .database import get_db
from .integrations.base import ConnectorError
from .integrations.credentials import decrypt_credentials, encrypt_credentials
from .models import Job, NetworkSetting, SourceAccount, utcnow
from . import queue_control

router = APIRouter(prefix='/pixiv')
KEY = 'pixiv_own_bookmark_download'


def policy(db):
    row = db.get(NetworkSetting, KEY)
    return decrypt_credentials(row.value_encrypted) if row else {'enabled': False}


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


@router.get('/collection-settings', dependencies=[Depends(require_admin_cookie)])
def get_policy(db=Depends(get_db)):
    return {'enabled': bool(policy(db).get('enabled')), 'scope': 'main_account_bookmarks'}


@router.put('/collection-settings', dependencies=[Depends(require_admin_cookie)])
def set_policy(value: PolicyInput, db=Depends(get_db)):
    queue_control.lock_control(db)
    current = policy(db)
    row = db.get(NetworkSetting, KEY)
    if row is None:
        row = NetworkSetting(key=KEY)
        db.add(row)
    row.value_encrypted = encrypt_credentials({**current, 'enabled': value.enabled})
    db.commit()
    return get_policy(db)


def queue_download(db, item, main):
    if item.assets or not policy(db).get('enabled'):
        return None
    from .jobs import enqueue_import
    return enqueue_import(db, {'platform': 'pixiv', 'remote_id': item.source_id, 'item_id': item.id,
                              'account_id': main, 'download': True, 'main_bookmark_download': True,
                              'pixiv_bookmark_state': {'account_id': main, 'bookmarked': True}},
                          commit=False, locked=True)


def schedule(db, *, force=False):
    config = policy(db)
    if not config.get('enabled'):
        return []
    from .pixiv_favorites import main_account_id
    main = main_account_id(db)
    if not main:
        return []
    active = db.scalar(select(Job.id).where(Job.kind == 'source_sync', Job.status.in_(('queued', 'running')),
                                            Job.payload['pixiv_bookmark_scan'].as_boolean().is_(True)))
    if active:
        return []
    last = config.get('last_started_at')
    try:
        recent = last and datetime.fromisoformat(last) > utcnow() - timedelta(hours=1)
    except (ValueError, TypeError):
        recent = False
    if recent and not force:
        return []
    batch = uuid.uuid4().hex
    rows = []
    for category in ('illustration', 'manga', 'novel'):
        for restrict in ('public', 'private'):
            row = Job(kind='source_sync', status='queued', payload={'platform': 'pixiv', 'account_id': main,
                      'main_account_id': main, 'pixiv_bookmark_scan': True, 'category': category,
                      'restrict': restrict, 'scan_batch': batch, 'page': 0, 'seen_cursors': []})
            db.add(row); rows.append(row)
    config['last_started_at'] = utcnow().isoformat()
    db.get(NetworkSetting, KEY).value_encrypted = encrypt_credentials(config)
    db.flush()
    return rows


def due(db):
    config = policy(db)
    if not config.get('enabled'):
        return False
    try:
        return not config.get('last_started_at') or datetime.fromisoformat(config['last_started_at']) <= utcnow() - timedelta(hours=1)
    except (TypeError, ValueError):
        return True


@router.post('/bookmarks/sync', dependencies=[Depends(require_admin_cookie)])
def request_scan(db=Depends(get_db)):
    queue_control.lock_control(db)
    rows = schedule(db, force=True)
    db.commit()
    return {'job_ids': [r.id for r in rows], 'scope': 'main_account_bookmarks'}


def scan_page(db, payload, report, guard):
    from .jobs import account_credentials, get_connector, merge_remote_item
    from .pixiv_favorites import main_account_id
    main = main_account_id(db)
    if main != payload.get('main_account_id') or not main:
        raise ConnectorError('Pixiv 主账号已更改，请重新同步主账号收藏')
    if not policy(db).get('enabled'):
        return {'skipped': True, 'message': '主账号收藏自动下载已关闭'}
    category, restrict = payload.get('category'), payload.get('restrict')
    if category not in ('illustration', 'manga', 'novel') or restrict not in ('public', 'private'):
        raise ConnectorError('主账号收藏扫描参数无效')
    connector = get_connector('pixiv', account_credentials(db, 'pixiv', main, purpose='metadata', pinned=True))
    db.commit()
    connector._api()
    ident = connector._positive_id(connector._authenticated_user_id)
    data = connector.fetch_author_page(ident, 'bookmarks', category, restrict, payload.get('cursor'), checkpoint=report)
    cursor = data['next_cursor']
    seen = list(payload.get('seen_cursors') or [])
    if cursor is not None:
        marker = hashlib.sha256(json.dumps(cursor, sort_keys=True).encode()).hexdigest()
        if marker in seen or payload.get('page', 0) >= 4999:
            raise ConnectorError('主账号收藏分页重复或超过 5000 页，已保留已获取内容')
        seen.append(marker)
    db.rollback()
    queue_control.lock_control(db)
    if main_account_id(db) != main:
        raise ConnectorError('Pixiv 主账号已更改，本页收藏未保存')
    if not policy(db).get('enabled'):
        return {'skipped': True, 'message': '主账号收藏自动下载已关闭'}
    for work in data['works']:
        merge_remote_item(db, 'pixiv', work, bookmark_account_id=main,
                          bookmark_hint={'account_id': main, 'bookmarked': True})
    if cursor is not None:
        identity = ':'.join((payload['scan_batch'], category, restrict, seen[-1]))
        exists = db.scalar(select(Job.id).where(Job.kind == 'source_sync', Job.payload['identity'].as_string() == identity))
        if not exists:
            db.add(Job(kind='source_sync', status='queued', payload={**{k: v for k, v in payload.items()
                       if k not in ('check_account_id', 'execution_account_id', 'retry_queue', 'retry_last_error')},
                       'identity': identity, 'cursor': cursor, 'page': payload.get('page', 0) + 1, 'seen_cursors': seen}))
    guard(db)
    db.commit()
    return {'scope': 'main_account_bookmarks', 'works': len(data['works']), 'unavailable': data['unavailable'],
            'has_more': cursor is not None, 'message': '主账号收藏已同步，缺失正文已进入自动下载队列'}


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    admin_password: str = Field(min_length=1, max_length=1024)


@router.post('/direct/credentials', dependencies=[Depends(require_auth)])
def direct_credentials(value: CredentialInput, request: Request, db=Depends(get_db)):
    try:
        check_password(value.admin_password, request)
    except HTTPException as exc:
        if exc.status_code == 401:
            raise HTTPException(403, '管理员密码错误，未导入 Pixiv 令牌') from None
        raise
    from .pixiv_favorites import main_account_id
    from .jobs import account_credentials
    main = main_account_id(db)
    if not main:
        raise HTTPException(409, '请先在服务器配置 Pixiv 主账号')
    try:
        credentials = account_credentials(db, 'pixiv', main, purpose='metadata', pinned=True)
    except ConnectorError as exc:
        raise HTTPException(409, str(exc)) from None
    token = credentials.get('refresh_token')
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{10,4096}', token):
        raise HTTPException(409, '主账号令牌无效，请重新授权主账号')
    account = db.get(SourceAccount, main)
    return JSONResponse({'account_id': main, 'account_label': account.label, 'refresh_token': token},
                        headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})
