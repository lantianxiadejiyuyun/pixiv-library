use crate::{
    ApiJson, AppState,
    db::{Tx, iso, now, truth},
    error::{ApiError, Result},
    executor,
    library::{self, Params, number, text},
    platforms, queue, secrets,
    tasks::{self, Context, TaskResult},
};
use axum::{
    Json,
    extract::{Path, Query, State},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::HashSet;
pub const KEY: &str = "pixiv_watchlist_sync";
fn defaults() -> Value {
    json!({"enabled":true,"interval_minutes":60,"account_id":null,"next_check_at":null,"last_checked_at":null,"last_error":null})
}
async fn state_tx(s: &AppState, tx: &mut Tx) -> Result<Value> {
    let mut v = defaults();
    let saved = executor::setting_tx(s, tx, KEY).await?;
    v.as_object_mut()
        .unwrap()
        .extend(saved.as_object().unwrap().clone());
    Ok(v)
}
async fn owner_tx(s: &AppState, tx: &mut Tx) -> Result<String> {
    let owner = executor::main_id(s, tx)
        .await?
        .ok_or_else(|| ApiError::bad("请先登录并配置 Pixiv 主账号"))?;
    if tx
        .query(
            "SELECT id FROM source_accounts WHERE id=? AND platform='pixiv'",
            &[json!(owner)],
        )
        .await?
        .is_empty()
    {
        return Err(ApiError::bad("Pixiv 主账号已移除"));
    }
    Ok(owner)
}
pub async fn queue_sync(s: &AppState, tx: &mut Tx, automatic: bool) -> Result<Option<Value>> {
    let owner = owner_tx(s, tx).await?;
    let mut state = state_tx(s, tx).await?;
    if automatic && !truth(&state["enabled"]) {
        return Ok(None);
    }
    let j=tx.query(&format!("SELECT * FROM jobs WHERE kind='source_sync' AND status IN ('queued','running') AND {} IN ('true','1') AND {}=? ORDER BY created_at,id LIMIT 1",tasks::json_text(s,"payload","watchlist"),tasks::json_text(s,"payload","account_id")),&[json!(owner)]).await?.pop();
    let j = if let Some(j) = j {
        j
    } else {
        tasks::enqueue_tx(
            s,
            tx,
            "source_sync",
            &json!({"platform":"pixiv","account_id":owner,"watchlist":true}),
        )
        .await?
    };
    state["account_id"] = json!(owner);
    state["next_check_at"] = json!(
        (chrono::Utc::now()
            + chrono::Duration::minutes(
                state["interval_minutes"]
                    .as_i64()
                    .unwrap_or(60)
                    .clamp(30, 10080)
            ))
        .naive_utc()
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string()
    );
    secrets::put_tx(s, tx, KEY, &state).await?;
    Ok(Some(j))
}
pub async fn schedule(s: &AppState, tx: &mut Tx) -> Result<()> {
    if tx
        .query(
            "SELECT `key` FROM network_settings WHERE `key`=?",
            &[json!(KEY)],
        )
        .await?
        .is_empty()
    {
        return Ok(());
    }
    let state = state_tx(s, tx).await?;
    if !truth(&state["enabled"]) {
        return Ok(());
    }
    let Ok(owner) = owner_tx(s, tx).await else {
        return Ok(());
    };
    if state["account_id"] != owner
        || tasks::date(&state["next_check_at"]).is_none_or(|d| d <= chrono::Utc::now().naive_utc())
    {
        queue_sync(s, tx, true).await?;
    }
    Ok(())
}
pub async fn queue_series(s: &AppState, tx: &mut Tx, entry: &Value) -> Result<Value> {
    let active=tx.query(&format!("SELECT * FROM jobs WHERE kind='series_sync' AND status IN ('queued','running') AND {}=? ORDER BY created_at,id LIMIT 1",tasks::json_text(s,"payload","watchlist_entry_id")),&[entry["id"].clone()]).await?.pop();
    if let Some(j) = active {
        return Ok(j);
    }
    tasks::enqueue_tx(s,tx,"series_sync",&json!({"platform":"pixiv","account_id":entry["account_id"],"remote_id":entry["remote_id"],"series_kind":entry["kind"],"watchlist_entry_id":entry["id"]})).await
}
pub async fn context(s: &AppState, tx: &mut Tx, p: &Value) -> Result<Option<Value>> {
    let Some(id) = p["watchlist_entry_id"].as_str() else {
        return Ok(None);
    };
    let owner = owner_tx(s, tx).await?;
    let e = tx
        .query("SELECT * FROM pixiv_watchlist WHERE id=?", &[json!(id)])
        .await?
        .pop()
        .filter(|e| e["account_id"] == owner && truth(&e["present_remote"]) && truth(&e["enabled"]))
        .ok_or_else(|| ApiError::bad("追更系列已暂停、移出列表或主账号已更改"))?;
    if e["remote_id"] != p["remote_id"] || e["kind"] != p["series_kind"] {
        return Err(ApiError::bad("追更系列任务不匹配"));
    }
    Ok(Some(e))
}
pub fn view(row: &Value) -> Value {
    let mut out = json!({});
    for key in [
        "id",
        "kind",
        "remote_id",
        "title",
        "author",
        "author_id",
        "content_count",
        "latest_content_id",
        "remote_updated_at",
        "series_id",
        "last_error",
    ] {
        out[key] = row[key].clone();
    }
    for key in ["present_remote", "enabled", "auto_download"] {
        out[key] = json!(truth(&row[key]));
    }
    out["has_update"] = json!(
        !row["latest_content_id"].is_null() && row["latest_content_id"] != row["seen_content_id"]
    );
    out["last_checked_at"] = iso(&row["last_checked_at"]);
    out
}
async fn owned(s: &AppState, tx: &mut Tx, id: &str) -> Result<Value> {
    let owner = owner_tx(s, tx).await?;
    tx.query(
        "SELECT * FROM pixiv_watchlist WHERE id=? AND account_id=?",
        &[json!(id), json!(owner)],
    )
    .await?
    .pop()
    .ok_or_else(|| ApiError::missing("追更系列不存在"))
}
pub async fn list(State(s): State<AppState>, Query(q): Query<Params>) -> Result<Json<Value>> {
    let page = number(&q, "page", 1, 1, 1000)?;
    let mut state = defaults();
    if let Some(old) = secrets::get(&s, KEY).await? {
        state
            .as_object_mut()
            .unwrap()
            .extend(old.as_object().unwrap().clone());
    }
    let roles = crate::accounts::roles(&s).await;
    let owner = if let Ok(r) = roles {
        if r.enabled {
            r.main_account_id
        } else {
            s.db.one("SELECT id FROM source_accounts WHERE platform='pixiv' ORDER BY created_at,id LIMIT 1",&[]).await?.and_then(|v|v["id"].as_str().map(str::to_owned))
        }
    } else {
        None
    };
    let mut condition = vec!["account_id=?".to_owned(), "present_remote=1".into()];
    let mut args = vec![json!(owner)];
    let kind = text(&q, "kind");
    if !kind.is_empty() {
        if !["novel", "manga"].contains(&kind) {
            return Err(ApiError::invalid("追更类型无效"));
        }
        condition.push("kind=?".into());
        args.push(json!(kind));
    }
    let term = text(&q, "q").trim();
    if term.chars().count() > 100 {
        return Err(ApiError::invalid("搜索内容过长"));
    }
    if !term.is_empty() {
        condition.push("(title LIKE ? ESCAPE '!' OR author LIKE ? ESCAPE '!')".into());
        args.extend(vec![json!(format!("%{}%", library::like(term))); 2]);
    }
    if library::flag(&q, "updates")? {
        condition.push("latest_content_id IS NOT NULL AND (seen_content_id IS NULL OR latest_content_id!=seen_content_id)".into());
    }
    let condition = condition.join(" AND ");
    let total =
        s.db.count(
            &format!("SELECT COUNT(*) FROM pixiv_watchlist WHERE {condition}"),
            &args,
        )
        .await?;
    args.push(json!((page - 1) * 30));
    let rows=s.db.query(&format!("SELECT * FROM pixiv_watchlist WHERE {condition} ORDER BY remote_updated_at DESC,id LIMIT 30 OFFSET ?"),&args).await?;
    let mut public = json!({});
    for key in ["enabled", "interval_minutes", "last_error"] {
        public[key] = state[key].clone();
    }
    for key in ["last_checked_at", "next_check_at"] {
        public[key] = iso(&state[key]);
    }
    public["configured"] = json!(owner.is_some());
    public["pending_tasks"] = json!(if let Some(owner) = owner {
        s.db.count(&format!("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running') AND {}=? AND ({} IN ('true','1') OR {} IS NOT NULL)",tasks::json_text(&s,"payload","account_id"),tasks::json_text(&s,"payload","watchlist"),tasks::json_text(&s,"payload","watchlist_entry_id")),&[json!(owner)]).await?
    } else {
        0
    });
    Ok(Json(
        json!({"items":rows.iter().map(view).collect::<Vec<_>>(),"total":total,"page":page,"page_size":30,"sync":public}),
    ))
}
pub async fn sync(State(s): State<AppState>) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let j = queue_sync(&s, &mut tx, false).await?.unwrap();
    tx.commit().await?;
    Ok(Json(queue::job_view(j)))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    enabled: bool,
    #[serde(default = "interval")]
    interval_minutes: i64,
}
fn interval() -> i64 {
    60
}
pub async fn settings(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<Settings>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    if !(30..=10080).contains(&v.interval_minutes) {
        return Err(ApiError::invalid("追更检查间隔须在 30–10080 分钟之间"));
    }
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let mut state = state_tx(&s, &mut tx).await?;
    state["enabled"] = json!(v.enabled);
    state["interval_minutes"] = json!(v.interval_minutes);
    secrets::put_tx(&s, &mut tx, KEY, &state).await?;
    tx.commit().await?;
    Ok(Json(
        json!({"enabled":v.enabled,"interval_minutes":v.interval_minutes}),
    ))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Edit {
    enabled: Option<bool>,
    auto_download: Option<bool>,
}
pub async fn edit(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(v): ApiJson<Edit>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let mut row = owned(&s, &mut tx, &id).await?;
    if let Some(v) = v.enabled {
        row["enabled"] = json!(v);
    }
    if let Some(v) = v.auto_download {
        row["auto_download"] = json!(v);
    }
    tx.execute(
        "UPDATE pixiv_watchlist SET enabled=?,auto_download=?,updated_at=? WHERE id=?",
        &[
            row["enabled"].clone(),
            row["auto_download"].clone(),
            json!(now()),
            json!(id),
        ],
    )
    .await?;
    if truth(&row["enabled"])
        && truth(&row["present_remote"])
        && (v.enabled == Some(true) || v.auto_download == Some(true))
    {
        queue_series(&s, &mut tx, &row).await?;
    }
    tx.commit().await?;
    Ok(Json(view(&row)))
}
pub async fn read(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let mut row = owned(&s, &mut tx, &id).await?;
    row["seen_content_id"] = row["latest_content_id"].clone();
    tx.execute(
        "UPDATE pixiv_watchlist SET seen_content_id=?,updated_at=? WHERE id=?",
        &[row["latest_content_id"].clone(), json!(now()), json!(id)],
    )
    .await?;
    tx.commit().await?;
    Ok(Json(view(&row)))
}
pub async fn check(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let row = owned(&s, &mut tx, &id).await?;
    if !truth(&row["present_remote"]) || !truth(&row["enabled"]) {
        return Err(ApiError(
            axum::http::StatusCode::CONFLICT,
            "请先恢复该系列追更",
        ));
    }
    let j = queue_series(&s, &mut tx, &row).await?;
    tx.commit().await?;
    Ok(Json(queue::job_view(j)))
}
pub async fn detail(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    let mut tx = s.db.begin().await?;
    let row = owned(&s, &mut tx, &id).await?;
    drop(tx);
    let mut v = view(&row);
    v["catalog"] = if let Some(series) = row["series_id"].as_str() {
        crate::series::get(State(s), Path(series.into())).await?.0
    } else {
        Value::Null
    };
    Ok(Json(v))
}
pub async fn process(ctx: Context, p: &Value) -> TaskResult<Value> {
    let s = &ctx.state;
    let (owner, c) =
        executor::identity(s, p, "check_account_id", "metadata", ctx.lane == "retry").await?;
    if owner.as_deref() != p["account_id"].as_str() {
        return Err(ApiError::bad("Pixiv 主账号已更改，旧账号追更结果未保存").into());
    }
    let api = platforms::pixiv::Pixiv::connect(s, &c, Some(ctx.clone())).await?;
    let records = api.watchlist().await?;
    let mut tx = s.db.begin().await?;
    ctx.guard_tx(&mut tx).await?;
    let current = owner_tx(s, &mut tx).await?;
    if owner.as_deref() != Some(&current) {
        return Err(ApiError::bad("Pixiv 主账号已更改，旧账号追更结果未保存").into());
    }
    let old = tx
        .query(
            "SELECT * FROM pixiv_watchlist WHERE account_id=?",
            &[json!(current)],
        )
        .await?;
    let mut seen = HashSet::new();
    let (mut added, mut changed, mut queued) = (0, 0, 0);
    for record in &records {
        let key = (
            record["kind"].as_str().unwrap().to_owned(),
            record["remote_id"].as_str().unwrap().to_owned(),
        );
        if !seen.insert(key.clone()) {
            return Err(ApiError::bad("追更系列重复，未保存列表").into());
        }
        let existing = old
            .iter()
            .find(|r| r["kind"] == record["kind"] && r["remote_id"] == record["remote_id"]);
        let needs = existing.is_none_or(|e| {
            e["latest_content_id"] != record["latest_content_id"]
                || e["content_count"] != record["content_count"]
                || !truth(&e["present_remote"])
                || e["last_checked_at"].is_null()
        });
        if existing.is_none() {
            added += 1;
        } else if existing.is_some_and(|e| e["latest_content_id"] != record["latest_content_id"]) {
            changed += 1;
        }
        let series = crate::series_sync::ensure_series(
            s,
            &mut tx,
            &record["remote_id"],
            &record["kind"],
            &record["title"],
        )
        .await?;
        let id = existing
            .map(|v| v["id"].as_str().unwrap().to_owned())
            .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
        let sql=format!("INSERT INTO pixiv_watchlist(id,account_id,kind,remote_id,title,author,author_id,content_count,latest_content_id,remote_updated_at,present_remote,enabled,auto_download,series_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,1,0,?,?,?) {}",s.db.upsert("title,author,author_id,content_count,latest_content_id,remote_updated_at,present_remote,series_id,updated_at","account_id,kind,remote_id"));
        tx.execute(
            &sql,
            &[
                json!(id),
                json!(current),
                record["kind"].clone(),
                record["remote_id"].clone(),
                record["title"].clone(),
                record["author"].clone(),
                record["author_id"].clone(),
                record["content_count"].clone(),
                record["latest_content_id"].clone(),
                record["remote_updated_at"].clone(),
                series["id"].clone(),
                json!(now()),
                json!(now()),
            ],
        )
        .await?;
        let entry = tx
            .query("SELECT * FROM pixiv_watchlist WHERE id=?", &[json!(id)])
            .await?
            .remove(0);
        if needs && truth(&entry["enabled"]) {
            queue_series(s, &mut tx, &entry).await?;
            queued += 1;
        }
    }
    for e in old {
        let key = (
            e["kind"].as_str().unwrap().to_owned(),
            e["remote_id"].as_str().unwrap().to_owned(),
        );
        if !seen.contains(&key) {
            tx.execute(
                "UPDATE pixiv_watchlist SET present_remote=0,updated_at=? WHERE id=?",
                &[json!(now()), e["id"].clone()],
            )
            .await?;
        }
    }
    let mut state = state_tx(s, &mut tx).await?;
    state["account_id"] = json!(current);
    state["last_checked_at"] = json!(now());
    state["last_error"] = Value::Null;
    state["next_check_at"] = json!(
        (chrono::Utc::now()
            + chrono::Duration::minutes(
                state["interval_minutes"]
                    .as_i64()
                    .unwrap_or(60)
                    .clamp(30, 10080)
            ))
        .naive_utc()
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string()
    );
    secrets::put_tx(s, &mut tx, KEY, &state).await?;
    tx.commit().await?;
    Ok(
        json!({"watchlist":true,"series_count":records.len(),"added":added,"updated":changed,"queued_series":queued,"message":"漫画与小说追更列表已同步，系列目录进入检查队列"}),
    )
}
