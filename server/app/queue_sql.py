"""Deterministic queue classification for an indexed generated column."""
from sqlalchemy import and_, case, func, or_


def lane_value(kind, payload):
    download = or_(payload['download'].as_string().is_(None), payload['download'].as_boolean())
    bundled = and_(kind == 'import', func.coalesce(payload['platform'].as_string(), '') == 'pixiv', download)
    manual = func.coalesce(payload['download_origin'].as_string(), '') == 'manual'
    retry = func.coalesce(payload['retry_queue'].as_boolean(), False).is_(True)
    return case((retry, 'retry'),
                (and_(kind.in_(('import', 'import_content')), manual), 'manual_download'),
                (or_(kind == 'import_content', bundled), 'download'),
                (kind == 'import', 'details'),
                (kind.in_(('source_sync', 'series_sync', 'author_profile', 'author_feed', 'pixiv_bookmark')), 'checks'),
                (kind == 'ai', 'ai'), (kind == 'translate', 'translation'), else_='other')
