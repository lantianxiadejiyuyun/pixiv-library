from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import jobs, queue_control
from app.database import SessionLocal
from app.integrations.base import ConnectorError, JobCancelled, RemoteWork
from app.integrations.credentials import encrypt_credentials
from app.integrations.pixiv import PixivConnector
from app.models import FavoriteGroup, FavoriteGroupItem, Item, ItemFavorite, Job, NetworkSetting, SourceAccount, Subscription
from app.pixiv_accounts import KEY, RolesInput
from app.pixiv_favorites import add_local_favorite, observe, process
from test_library import client


@pytest.fixture(autouse=True)
def worker_ready():
    jobs._stop.clear()


def accounts(db):
    rows = [SourceAccount(platform="pixiv", label=name,
        credentials_encrypted=encrypt_credentials({"refresh_token": name + "-test"}))
        for name in ("main", "download", "details", "author")]
    db.add_all(rows)
    db.flush()
    main, download, details, author = [row.id for row in rows]
    config = RolesInput(enabled=True, main_account_id=main, content_account_ids=[download],
        detail_account_ids=[details], check_account_ids=[author])
    db.add(NetworkSetting(key=KEY, value_encrypted=encrypt_credentials(config.model_dump())))
    db.commit()
    return main, download, details, author


def item(db, ident="42", favorite=False):
    record = Item(title="合成作品", source="pixiv", source_id=ident, type="illustration", user_score=4)
    db.add(record)
    if favorite:
        group = FavoriteGroup(name="手工分组")
        db.add(group)
        db.flush()
        record.favorite = ItemFavorite(groups=[FavoriteGroupItem(group_id=group.id)])
    db.commit()
    return record


def work(state, ident="42"):
    return RemoteWork(remote_id=ident, title="合成作品", source_url="https://www.pixiv.net/artworks/42",
                      metadata_only=True, is_bookmarked=state)


@pytest.mark.parametrize("flag,expected", [(True, True), (False, False), (None, None), (1, None), ("true", None)])
def test_only_boolean_pixiv_bookmark_flags_are_accepted(flag, expected):
    parsed = PixivConnector._metadata_work({"id": 42, "title": "合成作品", "page_count": 1, "is_bookmarked": flag}, novel=False)
    assert parsed.is_bookmarked is expected


def test_main_positive_adds_without_changing_score_group_or_favorite_order(client):
    with SessionLocal() as db:
        main, *_ = accounts(db)
        record = item(db, favorite=True)
        previous = record.favorite.created_at
        group_ids = [link.group_id for link in record.favorite.groups]
        add_local_favorite(db, record)
        add_local_favorite(db, record)
        observe(db, record, work(False), main)
        db.commit()
        assert record.favorite.created_at == previous
        assert [link.group_id for link in record.favorite.groups] == group_ids
        assert record.user_score == 4
        assert db.scalar(select(func.count()).select_from(ItemFavorite)) == 1


def test_false_main_observation_does_not_add_and_positive_does(client):
    with SessionLocal() as db:
        main, *_ = accounts(db)
        record = item(db)
        observe(db, record, work(False), main)
        db.commit()
        assert record.favorite is None
        observe(db, record, work(True), main)
        db.commit()
        assert record.favorite is not None and record.user_score == 4
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_auxiliary_bookmarks_are_not_owner_bookmarks_and_checks_are_deduplicated(client):
    with SessionLocal() as db:
        main, download, *_ = accounts(db)
        record = item(db)
        for state in (True, False, None):
            observe(db, record, work(state), download)
        db.commit()
        assert record.favorite is None
        queued = db.scalars(select(Job)).all()
        assert len(queued) == 1 and queued[0].kind == "pixiv_bookmark"
        assert queued[0].payload["main_account_id"] == main
        assert queue_control.job_lane(queued[0]) == "checks"


def test_trusted_discovery_hint_avoids_extra_main_requests(client):
    with SessionLocal() as db:
        main, download, *_ = accounts(db)
        record = item(db)
        observe(db, record, work(False), download, {"account_id": main, "bookmarked": True})
        db.commit()
        assert record.favorite is not None
        assert not db.scalars(select(Job)).all()


def test_wrong_identity_hint_is_not_trusted(client):
    with SessionLocal() as db:
        main, download, *_ = accounts(db)
        record = item(db)
        observe(db, record, work(True), download, {"account_id": download, "bookmarked": True})
        db.commit()
        assert record.favorite is None
        assert db.scalar(select(Job)).payload["main_account_id"] == main


@pytest.mark.parametrize("state", [True, False])
def test_real_worker_uses_main_identity_on_checks_lane(client, monkeypatch, state):
    with SessionLocal() as db:
        main, download, *_ = accounts(db)
        record = item(db)
        ident = record.id
        observe(db, record, work(True), download)
        db.commit()
    seen = []
    class Connector:
        def fetch(self, remote_id, *, download, include_preview, checkpoint):
            assert download is False and include_preview is False
            return work(state, remote_id)
    def factory(platform, credentials):
        assert platform == "pixiv"
        seen.append(credentials["refresh_token"])
        return Connector()
    monkeypatch.setattr(jobs, "get_connector", factory)
    assert jobs.run_once(("checks",))
    with SessionLocal() as db:
        completed = db.scalar(select(Job))
        assert completed.status == "completed", completed.error
        assert completed.payload["check_account_id"] == main
        assert jobs.job_view(completed)["label"] == "Pixiv 主账号收藏检查"
        assert (db.get(Item, ident).favorite is not None) == state
    assert seen == ["main-test"]


def test_lost_lease_rolls_back_automatic_favorite(client, monkeypatch):
    with SessionLocal() as db:
        main, *_ = accounts(db)
        record = item(db)
        ident = record.id
        payload = {"item_id": ident, "remote_id": "42", "account_id": main, "main_account_id": main}
        monkeypatch.setattr(jobs, "get_connector", lambda *_: SimpleNamespace(fetch=lambda *a, **kw: work(True)))
        with pytest.raises(JobCancelled):
            process(db, payload, lambda _: None, lambda _: (_ for _ in ()).throw(JobCancelled()))
        db.rollback()
        assert db.get(Item, ident).favorite is None


def test_changed_main_identity_cannot_apply_old_observation(client):
    with SessionLocal() as db:
        main, download, *_ = accounts(db)
        record = item(db)
        with pytest.raises(ConnectorError, match="主账号已更改"):
            process(db, {"item_id": record.id, "remote_id": "42", "main_account_id": download}, lambda _: None, lambda _: None)
        assert record.favorite is None


def test_explicit_retry_uses_current_main_and_deduplicates_checks(client):
    with SessionLocal() as db:
        main, download, *_ = accounts(db)
        record = item(db)
        failed = Job(kind="pixiv_bookmark", status="failed", payload={"platform": "pixiv",
            "item_id": record.id, "remote_id": "42", "main_account_id": download,
            "account_id": download, "check_account_id": download})
        db.add(failed); db.commit()
        ident, item_id = failed.id, record.id
    response = client.post(f"/api/jobs/{ident}/retry")
    assert response.status_code == 200
    with SessionLocal() as db:
        retried = db.get(Job, ident)
        assert retried.payload["main_account_id"] == main
        assert retried.payload["account_id"] == main
        assert "check_account_id" not in retried.payload
        other = Job(kind="pixiv_bookmark", status="failed", payload={"platform": "pixiv",
            "item_id": item_id, "remote_id": "42", "main_account_id": download})
        db.add(other); db.commit(); other_id = other.id
    assert client.post(f"/api/jobs/{other_id}/retry").json()["id"] == ident


def test_follows_root_uses_authenticated_main_identity_not_manual_user_id(monkeypatch):
    calls = []
    class API:
        def user_following(self, **params):
            calls.append(params)
            return {"user_previews": [{"user": {"id": 123, "name": "关注作者"}}]}
    connector = PixivConnector({})
    connector._authenticated_user_id, connector._user_id = "10", "999"
    monkeypatch.setattr(connector, "_api", lambda: API())
    assert {row["target"] for row in connector.follows()} == {"123"}
    assert {row["user_id"] for row in calls} == {"10"}
    assert {row["restrict"] for row in calls} == {"public", "private"}


@pytest.mark.parametrize("next_url", [
    "https://app-api.pixiv.net/v1/user/following?user_id=999&restrict=public&offset=30",
    "https://app-api.pixiv.net/v1/user/following?user_id=10&restrict=private&offset=30",
    "https://app-api.pixiv.net/v1/user/illusts?user_id=10&offset=30",
    "https://app-api.pixiv.net/v1/user/following?user_id=10&offset=30&offset=60",
])
def test_follow_pagination_cannot_change_root_or_directory(monkeypatch, next_url):
    connector = PixivConnector({})
    connector._authenticated_user_id = "10"
    calls = []
    def following(**params):
        calls.append(params)
        return {"user_previews": [], "next_url": next_url}
    monkeypatch.setattr(connector, "_api", lambda: SimpleNamespace(user_following=following))
    with pytest.raises(ConnectorError, match="主账号范围"):
        connector.follows()
    assert len(calls) == 1


def test_follow_sync_rejects_unknown_authenticated_identity(monkeypatch):
    connector = PixivConnector({"user_id": "999"})
    connector._user_id = "999"
    monkeypatch.setattr(connector, "_api", lambda: SimpleNamespace())
    with pytest.raises(ConnectorError, match="账号身份"):
        connector.follows()


def test_author_scope_includes_public_bookmarks_once_without_expanding_their_authors(client, monkeypatch):
    calls = []
    def entry(ident, state=False):
        return {"id": ident, "title": "合成作品", "is_bookmarked": state, "user": {"id": 999, "name": "未关注作者"}}
    class API:
        def user_illusts(self, uid, type):
            calls.append(("own", uid, type)); return {"illusts": [entry(1 if type == "illust" else 2, type == "illust")]}
        def user_bookmarks_illust(self, uid, restrict):
            calls.append(("bookmarks", uid, restrict)); return {"illusts": [entry(3)], "next_url": "https://app-api.pixiv.net/next"}
        def user_novels(self, uid):
            calls.append(("novel", uid)); return {"novels": [entry(4)]}
        def user_bookmarks_novel(self, uid, restrict):
            calls.append(("novel-bookmarks", uid, restrict)); return {"novels": [entry(5)]}
    connector = PixivConnector({})
    monkeypatch.setattr(connector, "_api", lambda: API())
    monkeypatch.setattr(jobs, "get_connector", lambda *_: connector)
    with SessionLocal() as db:
        main, *_ = accounts(db)
        positive = item(db, "1")
        negative = item(db, "3")
        rule = Subscription(platform="pixiv", target_type="author", target="123", label="关注作者",
            enabled=True, auto_download=False, content_scope="all", account_id=main)
        db.add(rule); db.commit()
        result = jobs._sync(db, {"platform": "pixiv", "subscription_id": rule.id, "check_account_id": main}, lambda _: None, lambda _: None)
        assert result["discovered"] == 5 and result["has_more"]
        assert positive.favorite is not None
        assert negative.favorite is None  # Another author's collection is not the main account's collection.
        assert db.scalar(select(func.count()).select_from(Subscription)) == 1
        queued = db.scalars(select(Job).where(Job.kind == "import")).all()
        assert {row.payload["remote_id"] for row in queued} == {"1", "2", "3", "novel:4", "novel:5"}
        assert all(row.payload["download"] is False for row in queued)
        assert next(row for row in queued if row.payload["remote_id"] == "1").payload["pixiv_bookmark_state"] == {"account_id": main, "bookmarked": True}
        assert "公开收藏" in result["message"]
        assert "不扩展" in client.get("/api/subscriptions").json()["items"][0]["status_message"]
    assert len(calls) == 5
    assert all(call[1] == "123" for call in calls)
    assert calls[2][-1] == "public" and calls[4][-1] == "public"
