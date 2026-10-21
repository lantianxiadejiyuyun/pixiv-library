import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Computed, DateTime, Float, ForeignKey, Index, Integer, BigInteger, String, Text, UniqueConstraint, column
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from .database import Base
from .job_account_sql import account_value, unique_account_value


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    # Naive UTC is portable across MySQL and SQLite; API explicitly emits UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Item(TimestampMixin, Base):
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_item_source_identity"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(512))
    title_zh: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title_zh_source: Mapped[str | None] = mapped_column(String(512), nullable=True)
    type: Mapped[str] = mapped_column(String(24), default="manga", index=True)
    source: Mapped[str] = mapped_column(String(24), default="local", index=True)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(512), default="")
    author_source_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    language: Mapped[str] = mapped_column(String(32), default="")
    rating: Mapped[str] = mapped_column(String(24), default="unknown", index=True)
    rating_origin: Mapped[str] = mapped_column(String(24), default="source")
    preview_cover: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    remote_page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_bookmark_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source_series: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    content_state: Mapped[str] = mapped_column(String(24), default="metadata", server_default="metadata", index=True)
    content_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    assets: Mapped[list["Asset"]] = relationship(back_populates="item", cascade="all, delete-orphan", order_by="Asset.position", lazy="selectin")
    item_tags: Mapped[list["ItemTag"]] = relationship(back_populates="item", cascade="all, delete-orphan", lazy="selectin")
    progress: Mapped["ReadingProgress | None"] = relationship(cascade="all, delete-orphan", uselist=False, lazy="selectin")
    favorite: Mapped["ItemFavorite | None"] = relationship(cascade="all, delete-orphan", uselist=False, lazy="selectin")


class FavoriteGroup(TimestampMixin, Base):
    __tablename__ = "favorite_groups"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    links: Mapped[list["FavoriteGroupItem"]] = relationship(cascade="all, delete-orphan")


class ItemFavorite(Base):
    __tablename__ = "item_favorites"
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    groups: Mapped[list["FavoriteGroupItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin", overlaps="links")


class FavoriteGroupItem(Base):
    __tablename__ = "favorite_group_items"
    group_id: Mapped[str] = mapped_column(ForeignKey("favorite_groups.id", ondelete="CASCADE"), primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("item_favorites.item_id", ondelete="CASCADE"), primary_key=True, index=True)


class ItemVisit(Base):
    __tablename__ = "item_visits"
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    viewed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("item_id", "position", name="uq_asset_item_position"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    relative_path: Mapped[str] = mapped_column(String(512))
    original_name: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), default="available")
    inline_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    item: Mapped[Item] = relationship(back_populates="assets")


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(191), unique=True)
    name_zh: Mapped[str | None] = mapped_column(String(191), nullable=True)


class ItemTag(Base):
    __tablename__ = "item_tags"
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[str] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)
    origin: Mapped[str] = mapped_column(String(24), primary_key=True, default="manual")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    item: Mapped[Item] = relationship(back_populates="item_tags")
    tag: Mapped[Tag] = relationship(lazy="joined")


class CharacterProfile(TimestampMixin, Base):
    __tablename__ = "character_profiles"
    tag_id: Mapped[str] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(24))
    provider_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(512))
    native_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    works: Mapped[list] = mapped_column(JSON, default=list)
    source_url: Mapped[str] = mapped_column(String(512))
    search_text: Mapped[str] = mapped_column(Text, default="")


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim_order", "status", "kind", "created_at", "id"),
                      Index("ix_jobs_lane_claim", "queue_lane", "status", "created_at", "id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    from .queue_sql import lane_value as _lane_value
    queue_lane: Mapped[str] = mapped_column(String(24), Computed(_lane_value(column('kind', String()), column('payload', JSON())), persisted=False))
    download_account_id: Mapped[str | None] = mapped_column(String(36), Computed(account_value(column('payload', JSON()), 'download_account_id'), persisted=False), deferred=True)
    details_account_id: Mapped[str | None] = mapped_column(String(36), Computed(account_value(column('payload', JSON()), 'details_account_id'), persisted=False), deferred=True)
    check_account_id: Mapped[str | None] = mapped_column(String(36), Computed(account_value(column('payload', JSON()), 'check_account_id'), persisted=False), deferred=True)
    execution_account_id: Mapped[str | None] = mapped_column(String(36), Computed(account_value(column('payload', JSON()), 'execution_account_id'), persisted=False), deferred=True)
    stats_details_account_id: Mapped[str | None] = mapped_column(String(36), Computed(unique_account_value('details_account_id'), persisted=False), deferred=True)
    stats_check_account_id: Mapped[str | None] = mapped_column(String(36), Computed(unique_account_value('check_account_id'), persisted=False), deferred=True)
    stats_execution_account_id: Mapped[str | None] = mapped_column(String(36), Computed(unique_account_value('execution_account_id'), persisted=False), deferred=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SourceAccount(TimestampMixin, Base):
    __tablename__ = "source_accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(24), index=True)
    label: Mapped[str] = mapped_column(String(191))
    credentials_encrypted: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class QueueControl(TimestampMixin, Base):
    __tablename__ = "queue_control"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    rate_limit_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    concurrency: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    download_account_cursor: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lanes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    min_interval_seconds: Mapped[float] = mapped_column(Float, default=2.0)
    max_interval_seconds: Mapped[float] = mapped_column(Float, default=5.0)
    next_request_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cover_streak: Mapped[int] = mapped_column(Integer, default=0)


class NetworkSetting(TimestampMixin, Base):
    __tablename__ = "network_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_encrypted: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), "mysql"))


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(24))
    target_type: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(512))
    label: Mapped[str] = mapped_column(String(191))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_download: Mapped[bool] = mapped_column(Boolean, default=False)
    download_works: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    download_bookmarks: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    content_scope: Mapped[str] = mapped_column(String(16), default="images", server_default="images")
    account_id: Mapped[str | None] = mapped_column(ForeignKey("source_accounts.id", ondelete="SET NULL"), nullable=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=360)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class PixivWatchlist(TimestampMixin, Base):
    __tablename__ = "pixiv_watchlist"
    __table_args__ = (UniqueConstraint("account_id", "kind", "remote_id", name="uq_pixiv_watchlist_owner_series"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("source_accounts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    remote_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(512))
    author: Mapped[str] = mapped_column(String(191), default="")
    author_id: Mapped[str] = mapped_column(String(32), default="")
    content_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_content_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    seen_content_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    remote_updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    present_remote: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_download: Mapped[bool] = mapped_column(Boolean, default=False)
    series_id: Mapped[str | None] = mapped_column(ForeignKey("series.id", ondelete="SET NULL"), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReadingProgress(Base):
    __tablename__ = "reading_progress"
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    page: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    offset: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Series(TimestampMixin, Base):
    __tablename__ = "series"
    __table_args__ = (UniqueConstraint("source", "source_id", "kind", name="uq_series_source_identity"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    origin: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(24))
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(512))
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    entries: Mapped[list["SeriesEntry"]] = relationship(cascade="all, delete-orphan", order_by="SeriesEntry.position", lazy="selectin")


class SeriesEntry(Base):
    __tablename__ = "series_entries"
    __table_args__ = (UniqueConstraint("series_id", "position", name="uq_series_entry_position"),)
    series_id: Mapped[str] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(512), default="")


class AuthorProfile(TimestampMixin, Base):
    __tablename__ = "author_profiles"
    remote_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    avatar: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    statistics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuthorFeed(TimestampMixin, Base):
    __tablename__ = "author_feeds"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope_key: Mapped[str] = mapped_column(String(64), unique=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("author_profiles.remote_id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("source_accounts.id", ondelete="CASCADE"))
    section: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(16))
    restrict: Mapped[str] = mapped_column(String(16), default="public")
    generation: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    next_cursor: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    seen_cursors: Mapped[list] = mapped_column(JSON, default=list)
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    unavailable: Mapped[int] = mapped_column(Integer, default=0)
    raw_count: Mapped[int] = mapped_column(Integer, default=0)


class AuthorFeedEntry(Base):
    __tablename__ = "author_feed_entries"
    feed_id: Mapped[str] = mapped_column(ForeignKey("author_feeds.id", ondelete="CASCADE"), primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)


class DevicePairing(Base):
    __tablename__ = "device_pairings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # One live authorization slot. Replacing it invalidates the previous QR code.
    slot: Mapped[int] = mapped_column(Integer, unique=True, default=1)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DeviceSession(Base):
    __tablename__ = "device_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    device_name: Mapped[str] = mapped_column(String(191))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AndroidRelease(Base):
    __tablename__ = "android_releases"
    version_code: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    version_name: Mapped[str] = mapped_column(String(80))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    certificate_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    min_sdk: Mapped[int] = mapped_column(Integer)
    relative_path: Mapped[str] = mapped_column(String(255))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


Index("ix_item_recent", Item.created_at)
Index("ix_item_source_recent", Item.source, Item.created_at.desc(), Item.id)
Index("ix_item_order_recent", Item.created_at.desc(), Item.id)
Index("ix_items_author_recent", Item.author, Item.created_at.desc(), Item.id)
Index("ix_items_source_author_type", Item.source, Item.author_source_id, Item.type, Item.created_at.desc(), Item.id)
Index("ix_author_feed_page", AuthorFeedEntry.feed_id, AuthorFeedEntry.generation.desc(), AuthorFeedEntry.position, AuthorFeedEntry.item_id)
Index("ix_jobs_kind_recent", Job.kind, Job.updated_at.desc(), Job.id.desc())
Index("ix_jobs_lane_recent", Job.queue_lane, Job.created_at.desc(), Job.id)
Index("ix_jobs_recent", Job.created_at.desc(), Job.id)
Index("ix_jobs_download_stats", Job.download_account_id, Job.status)
Index("ix_jobs_details_stats", Job.stats_details_account_id, Job.status)
Index("ix_jobs_check_stats", Job.stats_check_account_id, Job.status)
Index("ix_jobs_execution_stats", Job.stats_execution_account_id, Job.status)
Index("ix_assets_size", Asset.size_bytes)
Index("ix_item_tags_item", ItemTag.item_id)
