use crate::{
    ApiJson, AppState, content_store,
    db::{Tx, json_column, now, truth},
    error::{ApiError, Result},
    executor, library, platforms, queue,
    tasks::{self, Context, TaskResult},
};
use axum::{Json, extract::State};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::HashSet;
pub async fn ensure_series(
    _s: &AppState,
    tx: &mut Tx,
    remote: &Value,
    kind: &Value,
    title: &Value,
) -> Result<Value> {
    if let Some(v) = tx
        .query(
            "SELECT * FROM series WHERE source='pixiv' AND source_id=? AND kind=?",
            &[remote.clone(), kind.clone()],
        )
        .await?
        .pop()
    {
        return Ok(v);
    }
    let id = uuid::Uuid::new_v4().to_string();
    tx.execute("INSERT INTO series(id,origin,source,source_id,kind,title,complete,revision,created_at,updated_at) VALUES(?,'source','pixiv',?,?,?,0,0,?,?)",&[json!(id),remote.clone(),kind.clone(),title.clone(),json!(now()),json!(now())]).await?;
    Ok(tx
        .query("SELECT * FROM series WHERE id=?", &[json!(id)])
        .await?
        .remove(0))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Input {
    #[serde(default = "platform")]
    platform: String,
    url_or_id: String,
    #[serde(default)]
    kind: Option<String>,
    #[serde(default)]
    account_id: Option<String>,
}
fn platform() -> String {
    "pixiv".into()
}
pub async fn enqueue(s: &AppState, tx: &mut Tx, p: &Value) -> Result<Value> {
    let active=tx.query(&format!("SELECT * FROM jobs WHERE kind='series_sync' AND status IN ('queued','running') AND {}=? AND {}=? ORDER BY created_at,id LIMIT 1",tasks::json_text(s,"payload","remote_id"),tasks::json_text(s,"payload","series_kind")),&[p["remote_id"].clone(),p["series_kind"].clone()]).await?.pop();
    if let Some(j) = active {
        return Ok(j);
    }
    tasks::enqueue_tx(s, tx, "series_sync", p).await
}
pub async fn import(State(s): State<AppState>, ApiJson(v): ApiJson<Input>) -> Result<Json<Value>> {
    s.config.writable()?;
    if v.platform != "pixiv" || v.url_or_id.len() > 2048 {
        return Err(ApiError::invalid("系列来源或链接无效"));
    }
    let (remote, kind) = platforms::pixiv_series::normalize(&v.url_or_id, v.kind.as_deref())?;
    let owner =
        crate::accounts::resolve(&s, v.account_id.as_deref(), "metadata", false, false).await?;
    crate::accounts::validate_credentials(
        "pixiv",
        &crate::accounts::credentials(&s, "pixiv", owner.as_deref()).await?,
    )?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let j = enqueue(
        &s,
        &mut tx,
        &json!({"platform":"pixiv","remote_id":remote,"series_kind":kind,"account_id":owner}),
    )
    .await?;
    tx.commit().await?;
    Ok(Json(queue::job_view(j)))
}
pub async fn process(ctx: Context, p: &Value) -> TaskResult<Value> {
    let s = &ctx.state;
    let (owner, c) =
        executor::identity(s, p, "check_account_id", "metadata", ctx.lane == "retry").await?;
    let ident = p["remote_id"].as_str().unwrap_or("");
    let kind = p["series_kind"].as_str().unwrap_or("");
    let source_id = tasks::json_text(s, "source_series", "remote_id");
    let source_kind = tasks::json_text(s, "source_series", "kind");
    let known=s.db.query(&format!("SELECT i.id,i.source_id FROM items i WHERE i.source='pixiv' AND (i.id IN (SELECT e.item_id FROM series_entries e JOIN series s ON s.id=e.series_id WHERE s.source='pixiv' AND s.source_id=? AND s.kind=?) OR ({source_id}=? AND {source_kind}=?))"),&[json!(ident),json!(kind),json!(ident),json!(kind)]).await?;
    let api = platforms::pixiv::Pixiv::connect(s, &c, Some(ctx.clone())).await?;
    let catalog = api.series(ident, kind).await?;
    if catalog.id != ident
        || catalog.kind != kind
        || catalog.title.trim().is_empty()
        || catalog.title.chars().count() > 512
    {
        return Err(ApiError::bad("Pixiv 系列目录格式不完整").into());
    }
    let mut tx = s.db.begin().await?;
    ctx.guard_tx(&mut tx).await?;
    let watch = crate::watchlist::context(s, &mut tx, p).await?;
    let series = ensure_series(
        s,
        &mut tx,
        &json!(ident),
        &json!(kind),
        &json!(catalog.title),
    )
    .await?;
    let mut alive = HashSet::new();
    for chunk in known.chunks(200) {
        let args = chunk.iter().map(|v| v["id"].clone()).collect::<Vec<_>>();
        for v in tx
            .query(
                &format!(
                    "SELECT id FROM items WHERE id IN ({})",
                    library::placeholders(args.len())
                ),
                &args,
            )
            .await?
        {
            alive.insert(v["id"].as_str().unwrap().to_owned());
        }
    }
    let deleted: HashSet<_> = known
        .iter()
        .filter(|v| !alive.contains(v["id"].as_str().unwrap()))
        .filter_map(|v| v["source_id"].as_str())
        .collect();
    let mut entries = vec![];
    let mut queued = vec![];
    let download = watch.as_ref().is_some_and(|w| truth(&w["auto_download"]));
    for w in &catalog.works {
        if deleted.contains(w.remote_id.as_str()) {
            continue;
        }
        let item = content_store::merge(s, &mut tx, "pixiv", w).await?;
        executor::observe(
            s,
            &mut tx,
            item["id"].as_str().unwrap(),
            w,
            owner.as_deref(),
            &Value::Null,
        )
        .await?;
        entries.push(item["id"].clone());
        if tx
            .query(
                "SELECT id FROM assets WHERE item_id=? LIMIT 1",
                &[item["id"].clone()],
            )
            .await?
            .is_empty()
            && (json_column(&item["preview_cover"]).is_null() || download)
        {
            let j=tasks::enqueue_import_tx(s,&mut tx,json!({"platform":"pixiv","remote_id":w.remote_id,"item_id":item["id"],"account_id":p["account_id"],"download":download})).await?;
            queued.push(j["id"].clone());
        }
    }
    let complete = catalog.complete && deleted.is_empty();
    let count = tx
        .query(
            "SELECT COUNT(*) AS n FROM series_entries WHERE series_id=?",
            &[series["id"].clone()],
        )
        .await?[0]["n"]
        .as_i64()
        .unwrap_or(0);
    let updated = complete || count == 0;
    if updated {
        tx.execute(
            "DELETE FROM series_entries WHERE series_id=?",
            &[series["id"].clone()],
        )
        .await?;
        for (position, id) in entries.iter().enumerate() {
            tx.execute(
                "INSERT INTO series_entries(series_id,item_id,position,label) VALUES(?,?,?,'')",
                &[series["id"].clone(), id.clone(), json!(position)],
            )
            .await?;
        }
        tx.execute(
            "UPDATE series SET title=?,complete=?,revision=revision+1,updated_at=? WHERE id=?",
            &[
                json!(catalog.title),
                json!(complete),
                json!(now()),
                series["id"].clone(),
            ],
        )
        .await?;
    } else if !deleted.is_empty() {
        tx.execute(
            "UPDATE series SET complete=0,revision=revision+1,updated_at=? WHERE id=?",
            &[json!(now()), series["id"].clone()],
        )
        .await?;
    }
    if let Some(watch) = watch {
        tx.execute("UPDATE pixiv_watchlist SET series_id=?,last_checked_at=?,last_error=NULL,updated_at=? WHERE id=?",&[series["id"].clone(),json!(now()),json!(now()),watch["id"].clone()]).await?;
    }
    tx.commit().await?;
    Ok(
        json!({"series_id":series["id"],"entry_count":if updated{entries.len() as i64}else{count},"complete":if updated{complete}else{truth(&series["complete"])&&deleted.is_empty()},"directory_updated":updated,"queued_jobs":queued,"skipped_deleted":deleted.len(),"message":if download{"追更目录已检查，缺失正文进入自动下载队列"}else if updated{"系列目录已保存，缺失封面已排队，未下载正文"}else{"远端仅返回部分目录，已有目录保留"}}),
    )
}
