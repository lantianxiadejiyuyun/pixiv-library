use crate::{
    AppState, content_store,
    db::{Tx, json_column, now, truth},
    error::{ApiError, Result},
    executor, platforms, queue, secrets, subscriptions,
    tasks::{self, Context, TaskResult},
};
use serde_json::{Value, json};
use std::collections::HashSet;
pub async fn queue_bookmark_download(
    s: &AppState,
    tx: &mut Tx,
    item: &str,
    main: &str,
) -> Result<()> {
    if !truth(&executor::setting_tx(s, tx, subscriptions::BOOKMARK_KEY).await?["enabled"]) {
        return Ok(());
    }
    if !tx
        .query(
            "SELECT id FROM assets WHERE item_id=? LIMIT 1",
            &[json!(item)],
        )
        .await?
        .is_empty()
    {
        return Ok(());
    }
    if let Some(i) = tx
        .query(
            "SELECT source_id FROM items WHERE id=? AND source='pixiv'",
            &[json!(item)],
        )
        .await?
        .pop()
    {
        tasks::enqueue_import_tx(s,tx,json!({"platform":"pixiv","remote_id":i["source_id"],"item_id":item,"account_id":main,"download":true,"main_bookmark_download":true,"pixiv_bookmark_state":{"account_id":main,"bookmarked":true}})).await?;
    }
    Ok(())
}
pub async fn schedule_bookmarks(s: &AppState, tx: &mut Tx, force: bool) -> Result<Vec<Value>> {
    let mut policy = executor::setting_tx(s, tx, subscriptions::BOOKMARK_KEY).await?;
    if !truth(&policy["enabled"]) {
        return Ok(vec![]);
    }
    let Some(main) = executor::main_id(s, tx).await? else {
        return Ok(vec![]);
    };
    let active=tx.query(&format!("SELECT id FROM jobs WHERE kind='source_sync' AND status IN ('queued','running') AND {} IN ('true','1') LIMIT 1",tasks::json_text(s,"payload","pixiv_bookmark_scan")),&[]).await?;
    if !active.is_empty() {
        return Ok(vec![]);
    }
    if !force
        && tasks::date(&policy["last_started_at"])
            .is_some_and(|d| d > chrono::Utc::now().naive_utc() - chrono::Duration::hours(1))
    {
        return Ok(vec![]);
    }
    let batch = uuid::Uuid::new_v4().simple().to_string();
    let mut rows = vec![];
    for category in ["illustration", "manga", "novel"] {
        for restrict in ["public", "private"] {
            rows.push(tasks::enqueue_tx(s,tx,"source_sync",&json!({"platform":"pixiv","account_id":main,"main_account_id":main,"pixiv_bookmark_scan":true,"category":category,"restrict":restrict,"scan_batch":batch,"page":0,"seen_cursors":[]})).await?);
        }
    }
    policy["last_started_at"] = json!(now());
    secrets::put_tx(s, tx, subscriptions::BOOKMARK_KEY, &policy).await?;
    Ok(rows)
}
pub async fn bookmark_scan(State(s): axum::extract::State<AppState>) -> Result<axum::Json<Value>> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let jobs = schedule_bookmarks(&s, &mut tx, true).await?;
    tx.commit().await?;
    Ok(axum::Json(
        json!({"job_ids":jobs.iter().map(|j|j["id"].clone()).collect::<Vec<_>>(),"scope":"main_account_bookmarks"}),
    ))
}
use axum::extract::State;
pub async fn scan_page(ctx: Context, p: &Value) -> TaskResult<Value> {
    let s = &ctx.state;
    let (owner, c) =
        executor::identity(s, p, "check_account_id", "metadata", ctx.lane == "retry").await?;
    if owner.as_deref() != p["main_account_id"].as_str() {
        return Err(ApiError::bad("Pixiv 主账号已更改，请重新同步主账号收藏").into());
    }
    if !crate::secrets::get(s, subscriptions::BOOKMARK_KEY)
        .await?
        .is_some_and(|v| truth(&v["enabled"]))
    {
        return Ok(json!({"skipped":true,"message":"主账号收藏自动下载已关闭"}));
    }
    let api = platforms::pixiv::Pixiv::connect(s, &c, Some(ctx.clone())).await?;
    let page = api
        .author_page(
            api.user(),
            "bookmarks",
            p["category"].as_str().unwrap_or(""),
            p["restrict"].as_str().unwrap_or(""),
            p.get("cursor").filter(|v| !v.is_null()),
        )
        .await?;
    let mut seen = p["seen_cursors"].as_array().cloned().unwrap_or_default();
    if let Some(next) = &page.next {
        let marker = json!(crate::authors::cursor_marker(next));
        if seen.contains(&marker) || p["page"].as_u64().unwrap_or(0) >= 4999 {
            return Err(ApiError::bad("主账号收藏分页重复或超过 5000 页，已保留已获取内容").into());
        }
        seen.push(marker);
    }
    let mut tx = s.db.begin().await?;
    ctx.guard_tx(&mut tx).await?;
    let main = executor::main_id(s, &mut tx).await?;
    if main != owner {
        return Err(ApiError::bad("Pixiv 主账号已更改，本页收藏未保存").into());
    }
    if !truth(&executor::setting_tx(s, &mut tx, subscriptions::BOOKMARK_KEY).await?["enabled"]) {
        return Ok(json!({"skipped":true,"message":"主账号收藏自动下载已关闭"}));
    }
    for w in &page.works {
        let i = content_store::merge(s, &mut tx, "pixiv", w).await?;
        executor::add_favorite(s, &mut tx, i["id"].as_str().unwrap()).await?;
        queue_bookmark_download(
            s,
            &mut tx,
            i["id"].as_str().unwrap(),
            main.as_deref().unwrap(),
        )
        .await?;
    }
    if let Some(next) = &page.next {
        let identity = format!(
            "{}:{}:{}:{}",
            p["scan_batch"].as_str().unwrap_or(""),
            p["category"].as_str().unwrap_or(""),
            p["restrict"].as_str().unwrap_or(""),
            seen.last().unwrap().as_str().unwrap()
        );
        if tx
            .query(
                &format!(
                    "SELECT id FROM jobs WHERE kind='source_sync' AND {}=? LIMIT 1",
                    tasks::json_text(s, "payload", "identity")
                ),
                &[json!(identity)],
            )
            .await?
            .is_empty()
        {
            let mut payload = p.clone();
            for key in [
                "check_account_id",
                "execution_account_id",
                "retry_queue",
                "retry_last_error",
            ] {
                payload.as_object_mut().unwrap().remove(key);
            }
            payload["identity"] = json!(identity);
            payload["cursor"] = next.clone();
            payload["page"] = json!(p["page"].as_u64().unwrap_or(0) + 1);
            payload["seen_cursors"] = json!(seen);
            tasks::enqueue_tx(s, &mut tx, "source_sync", &payload).await?;
        }
    }
    tx.commit().await?;
    Ok(
        json!({"scope":"main_account_bookmarks","works":page.works.len(),"unavailable":page.unavailable,"has_more":page.next.is_some(),"message":"主账号收藏已同步，缺失正文已进入自动下载队列"}),
    )
}
pub async fn source_sync(ctx: Context, p: &Value) -> TaskResult<Value> {
    if p["pixiv_bookmark_scan"] == true {
        return scan_page(ctx, p).await;
    }
    if p["watchlist"] == true {
        return crate::watchlist::process(ctx, p).await;
    }
    let s = &ctx.state;
    let platform = p["platform"].as_str().unwrap_or("");
    let sub = if let Some(id) = p["subscription_id"].as_str() {
        let sub =
            s.db.one("SELECT * FROM subscriptions WHERE id=?", &[json!(id)])
                .await?
                .filter(|v| truth(&v["enabled"]));
        if sub.is_none() {
            return Ok(json!({"skipped":true,"message":"订阅已暂停或删除"}));
        }
        sub
    } else {
        None
    };
    let (owner, c) =
        executor::identity(s, p, "check_account_id", "metadata", ctx.lane == "retry").await?;
    if let Some(sub) = sub {
        let kind = sub["target_type"].as_str().unwrap();
        let target = sub["target"].as_str().unwrap();
        let scope = sub["content_scope"].as_str().unwrap_or("images");
        let mut discoveries: Vec<platforms::pixiv_catalog::Discovery> = vec![];
        let has_more;
        if platform == "pixiv" {
            let api = platforms::pixiv::Pixiv::connect(s, &c, Some(ctx.clone())).await?;
            discoveries = api.discover(kind, target, scope).await?;
            has_more = discoveries.iter().any(|d| d.has_more);
        } else if platform == "picacg" {
            let api = platforms::picacg::Picacg::connect(s, &c, Some(ctx.clone())).await?;
            let response = api
                .request(
                    "comics/advanced-search?page=1",
                    reqwest::Method::POST,
                    Some(json!({"keyword":target,"categories":[],"s":"dd"})),
                )
                .await?;
            let entries = response["comics"]["docs"]
                .as_array()
                .filter(|v| v.len() <= 500)
                .ok_or_else(|| ApiError::bad("哔咔搜索响应格式无效"))?;
            has_more = response["comics"]["pages"].as_i64().unwrap_or(1) > 1;
            for e in entries {
                if kind == "tag"
                    && !e["tags"]
                        .as_array()
                        .is_some_and(|v| v.contains(&json!(target)))
                {
                    continue;
                }
                discoveries.push(discovery(platforms::picacg::normalize(
                    e["_id"].as_str().unwrap_or(""),
                )?));
            }
        } else if platform == "kemono" {
            let api = platforms::kemono::Kemono::connect(s, Some(ctx.clone())).await?;
            let path = if kind == "author" {
                let id = platforms::kemono::normalize(target, true)?;
                let p: Vec<_> = id.split(':').collect();
                format!("{}/user/{}/posts?o=0", p[0], p[1])
            } else {
                format!(
                    "posts?{}",
                    url::form_urlencoded::Serializer::new(String::new())
                        .append_pair("o", "0")
                        .append_pair(if kind == "tag" { "tag" } else { "q" }, target)
                        .finish()
                )
            };
            let response = api.request(&path).await?;
            let entries = response[if kind == "author" { "results" } else { "posts" }]
                .as_array()
                .filter(|v| v.len() <= 50)
                .ok_or_else(|| ApiError::bad("Kemono 分页数据不完整"))?;
            let count = if kind == "author" {
                &response["props"]["count"]
            } else {
                &response["count"]
            };
            let count = count
                .as_u64()
                .filter(|n| *n >= entries.len() as u64 && (*n == 0 || !entries.is_empty()))
                .ok_or_else(|| ApiError::bad("Kemono 分页计数无效"))?;
            has_more = count > entries.len() as u64;
            for e in entries {
                let id = platforms::kemono::normalize(
                    &format!(
                        "{}:{}:{}",
                        e["service"].as_str().unwrap_or(""),
                        e["user"].as_str().unwrap_or(""),
                        e["id"].as_str().unwrap_or("")
                    ),
                    false,
                )?;
                if kind == "author"
                    && id.rsplit_once(':').unwrap().0 != platforms::kemono::normalize(target, true)?
                {
                    return Err(ApiError::bad("Kemono 作者目录混入其他作者作品").into());
                }
                discoveries.push(discovery(id));
            }
        } else {
            return Err(ApiError::bad("此平台的定时检查暂不支持").into());
        }
        let mut tx = s.db.begin().await?;
        ctx.guard_tx(&mut tx).await?;
        let current = tx
            .query(
                "SELECT * FROM subscriptions WHERE id=?",
                &[sub["id"].clone()],
            )
            .await?
            .pop()
            .filter(|v| truth(&v["enabled"]));
        let Some(current) = current else {
            return Ok(json!({"skipped":true,"message":"订阅已暂停或删除"}));
        };
        if current["target"] != sub["target"] || current["platform"] != sub["platform"] {
            return Err(ApiError::bad("订阅目标已更改，旧结果未保存").into());
        }
        let default =
            truth(&executor::setting_tx(s, &mut tx, subscriptions::FOLLOW_KEY).await?["enabled"]);
        let (works, bookmarks) = subscriptions::flags(&current, default);
        let mut queued = vec![];
        let mut seen = HashSet::new();
        for d in &discoveries {
            if !seen.insert(&d.id) {
                continue;
            }
            let download = if platform == "pixiv" && kind == "author" {
                d.origins.contains("works") && works || d.origins.contains("bookmarks") && bookmarks
            } else {
                truth(&current["auto_download"])
            };
            let existing = tx
                .query(
                    "SELECT * FROM items WHERE source=? AND source_id=?",
                    &[json!(platform), json!(d.id)],
                )
                .await?
                .pop();
            let hint = if platform == "pixiv" {
                d.bookmarked
                    .map(|b| json!({"account_id":owner,"bookmarked":b}))
                    .unwrap_or(Value::Null)
            } else {
                Value::Null
            };
            if let Some(i) = &existing {
                if platform == "pixiv"
                    && d.bookmarked == Some(true)
                    && owner == executor::main_id(s, &mut tx).await?
                {
                    executor::add_favorite(s, &mut tx, i["id"].as_str().unwrap()).await?;
                    queue_bookmark_download(
                        s,
                        &mut tx,
                        i["id"].as_str().unwrap(),
                        owner.as_deref().unwrap(),
                    )
                    .await?;
                }
                if !tx
                    .query(
                        "SELECT id FROM assets WHERE item_id=? LIMIT 1",
                        &[i["id"].clone()],
                    )
                    .await?
                    .is_empty()
                    || !download && !json_column(&i["preview_cover"]).is_null()
                {
                    continue;
                }
            }
            let j=tasks::enqueue_import_tx(s,&mut tx,json!({"platform":platform,"remote_id":d.id,"account_id":current["account_id"],"download":download,"pixiv_bookmark_state":hint})).await?;
            queued.push(j["id"].clone());
        }
        tx.execute(
            "UPDATE subscriptions SET last_checked_at=?,last_error=NULL,updated_at=? WHERE id=?",
            &[json!(now()), json!(now()), current["id"].clone()],
        )
        .await?;
        tx.commit().await?;
        return Ok(
            json!({"subscription_id":current["id"],"discovered":seen.len(),"queued_jobs":queued,"latest_page_only":true,"has_more":has_more,"message":if platform=="pixiv"&&kind=="author"{"已检查作者作品及公开收藏，不扩展收藏作品的作者"}else{"已检查最新一页，历史补全尚未实现"}}),
        );
    }
    if platform == "picacg" {
        let api = platforms::picacg::Picacg::connect(s, &c, Some(ctx.clone())).await?;
        let entries = api.pages("users/favourite?s=ua", "comics").await?;
        let mut ids = HashSet::new();
        for e in entries {
            ids.insert(platforms::picacg::normalize(
                e["_id"].as_str().unwrap_or(""),
            )?);
        }
        let mut tx = s.db.begin().await?;
        ctx.guard_tx(&mut tx).await?;
        let mut queued = vec![];
        for id in &ids {
            let exists = tx
                .query(
                    "SELECT id,preview_cover FROM items WHERE source='picacg' AND source_id=?",
                    &[json!(id)],
                )
                .await?
                .pop();
            if let Some(i) = exists
                && (!json_column(&i["preview_cover"]).is_null()
                    || !tx
                        .query(
                            "SELECT id FROM assets WHERE item_id=? LIMIT 1",
                            &[i["id"].clone()],
                        )
                        .await?
                        .is_empty())
            {
                continue;
            }
            queued.push(
                tasks::enqueue_import_tx(
                    s,
                    &mut tx,
                    json!({"platform":"picacg","remote_id":id,"account_id":owner,"download":false}),
                )
                .await?["id"]
                    .clone(),
            );
        }
        tx.commit().await?;
        return Ok(
            json!({"remote_favorites":ids.len(),"queued_jobs":queued,"message":"收藏已排队导入元数据，未自动下载"}),
        );
    }
    if platform != "pixiv" {
        return Err(ApiError::bad("此平台关注同步待实现").into());
    }
    let api = platforms::pixiv::Pixiv::connect(s, &c, Some(ctx.clone())).await?;
    let follows = api.follows().await?;
    let mut tx = s.db.begin().await?;
    ctx.guard_tx(&mut tx).await?;
    let mut added = 0;
    let mut watchlist = Value::Null;
    for (id, label) in &follows {
        if !tx.query("SELECT id FROM subscriptions WHERE platform='pixiv' AND target_type='author' AND target=? LIMIT 1",&[json!(id)]).await?.is_empty(){continue;}
        let row = json!({"id":uuid::Uuid::new_v4().to_string(),"platform":"pixiv","target_type":"author","target":id,"label":label,"enabled":true,"auto_download":false,"download_works":null,"download_bookmarks":null,"content_scope":"all","account_id":owner,"interval_minutes":360,"last_checked_at":null,"next_check_at":(chrono::Utc::now()+chrono::Duration::minutes(5+added)).naive_utc().format("%Y-%m-%d %H:%M:%S%.6f").to_string(),"last_error":null,"created_at":now(),"updated_at":now()});
        subscriptions::insert(&mut tx, &row).await?;
        added += 1;
    }
    if let Some(j) = crate::watchlist::queue_sync(s, &mut tx, true).await? {
        watchlist = j["id"].clone();
    }
    tx.commit().await?;
    Ok(
        json!({"added_subscriptions":added,"remote_follows":follows.len(),"watchlist_job_id":watchlist,"message":"关注作者已导入为本地订阅，作品与作者收藏分别控制下载"}),
    )
}
fn discovery(id: String) -> platforms::pixiv_catalog::Discovery {
    platforms::pixiv_catalog::Discovery {
        id,
        origins: HashSet::from(["works".into()]),
        bookmarked: None,
        has_more: false,
    }
}
pub async fn tick(s: &AppState) -> Result<()> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    let control = queue::locked(s, &mut tx).await?;
    if truth(&control["paused"]) || truth(&queue::lane(&control, "checks")["paused"]) {
        tx.commit().await?;
        return Ok(());
    }
    let (filter, mut args) = queue::source_filter(&control, "platform");
    args.insert(0, json!(now()));
    let due=tx.query(&format!("SELECT * FROM subscriptions WHERE enabled=1 AND platform IN ('pixiv','picacg','kemono') AND (next_check_at IS NULL OR next_check_at<=?) AND ({filter}) ORDER BY next_check_at,created_at,id LIMIT 20"),&args).await?;
    for row in due {
        if !subscriptions::ready(row["platform"].as_str().unwrap_or("")) {
            continue;
        }
        subscriptions::enqueue_check(s, &mut tx, &row).await?;
        let interval = row["interval_minutes"]
            .as_i64()
            .unwrap_or(360)
            .clamp(30, 10080);
        let next = (chrono::Utc::now() + chrono::Duration::minutes(interval))
            .naive_utc()
            .format("%Y-%m-%d %H:%M:%S%.6f")
            .to_string();
        tx.execute(
            "UPDATE subscriptions SET next_check_at=?,updated_at=? WHERE id=?",
            &[json!(next), json!(now()), row["id"].clone()],
        )
        .await?;
    }
    if queue::source_enabled(&control, "pixiv") {
        schedule_bookmarks(s, &mut tx, false).await?;
        crate::watchlist::schedule(s, &mut tx).await?;
    }
    tx.commit().await?;
    Ok(())
}
