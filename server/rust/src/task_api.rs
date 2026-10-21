use crate::{
    AppState,
    db::{json_column, now},
    error::{ApiError, Result},
    queue, tasks,
};
use axum::{
    Json,
    extract::{Path, State},
    http::StatusCode,
};
use serde_json::{Value, json};
pub async fn retry(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    let _ = queue::locked(&s, &mut tx).await?;
    let mut j = tx
        .query(
            &format!(
                "SELECT * FROM jobs WHERE id=?{}",
                if s.db.mysql() { " FOR UPDATE" } else { "" }
            ),
            &[json!(id)],
        )
        .await?
        .pop()
        .ok_or_else(|| ApiError::missing("任务不存在"))?;
    if !matches!(j["status"].as_str(), Some("failed" | "cancelled")) {
        return Err(ApiError(StatusCode::CONFLICT, "只能重试失败或已取消的任务"));
    }
    let mut p = json_column(&j["payload"]);
    let kind = j["kind"].as_str().unwrap_or("").to_owned();
    if kind == "pixiv_bookmark" {
        let main = crate::executor::main_id(&s, &mut tx)
            .await?
            .ok_or(ApiError(StatusCode::CONFLICT, "请先配置 Pixiv 主账号"))?;
        if let Some(duplicate)=tx.query(&format!("SELECT * FROM jobs WHERE id<>? AND kind='pixiv_bookmark' AND status IN ('queued','running') AND {}=? AND {}=? LIMIT 1",tasks::json_text(&s,"payload","item_id"),tasks::json_text(&s,"payload","main_account_id")),&[json!(id),p["item_id"].clone(),json!(main)]).await?.pop(){tx.commit().await?;return Ok(Json(queue::job_view(duplicate)));}
        p["main_account_id"] = json!(main);
        p["account_id"] = json!(main);
    }
    if matches!(kind.as_str(), "import" | "import_content") {
        let duplicate=tx.query(&format!("SELECT * FROM jobs WHERE id<>? AND kind IN ('import','import_content') AND status IN ('queued','running') AND {}=? AND {}=? ORDER BY created_at,id LIMIT 1",tasks::json_text(&s,"payload","platform"),tasks::json_text(&s,"payload","remote_id")),&[json!(id),p["platform"].clone(),p["remote_id"].clone()]).await?.pop();
        if let Some(d) = duplicate {
            tx.commit().await?;
            return Ok(Json(queue::job_view(d)));
        }
        if let Some(item) = p["item_id"].as_str() {
            tx.execute("UPDATE items SET content_job_id=?,updated_at=? WHERE id=? AND NOT EXISTS(SELECT 1 FROM assets WHERE assets.item_id=items.id)",&[json!(id),json!(now()),json!(item)]).await?;
        }
    }
    if kind == "author_feed" {
        let feed = tx
            .query(
                "SELECT revision FROM author_feeds WHERE id=?",
                &[p["feed_id"].clone()],
            )
            .await?
            .pop()
            .ok_or(ApiError(
                StatusCode::CONFLICT,
                "作者目录或账号已删除，请重新打开作者主页",
            ))?;
        p["revision"] = feed["revision"].clone();
    }
    if kind == "series_sync"
        && let Some(d)=tx.query(&format!("SELECT * FROM jobs WHERE id<>? AND kind='series_sync' AND status IN ('queued','running') AND {}=? AND {}=? LIMIT 1",tasks::json_text(&s,"payload","remote_id"),tasks::json_text(&s,"payload","series_kind")),&[json!(id),p["remote_id"].clone(),p["series_kind"].clone()]).await?.pop(){tx.commit().await?;return Ok(Json(queue::job_view(d)));}
    if matches!(kind.as_str(), "ai" | "translate")
        && tx
            .query("SELECT id FROM items WHERE id=?", &[p["item_id"].clone()])
            .await?
            .is_empty()
    {
        return Err(ApiError(StatusCode::CONFLICT, "作品已移除，请刷新后重试"));
    }
    if !p.is_object() {
        return Err(ApiError::bad("任务参数格式已损坏，请重新创建任务"));
    }
    p["retry_queue"] = json!(true);
    p["retry_last_error"] = j["error"]
        .as_str()
        .filter(|v| !v.is_empty())
        .map(|v| json!(v))
        .unwrap_or_else(|| p["retry_last_error"].clone());
    if p["platform"] == "pixiv"
        || matches!(
            kind.as_str(),
            "author_profile" | "author_feed" | "series_sync"
        )
    {
        for key in crate::accounts::FIELDS {
            p.as_object_mut().unwrap().remove(key);
        }
    }
    tx.execute("UPDATE jobs SET status='queued',error=NULL,result=NULL,progress=0,payload=?,lease_until=NULL,worker_id=NULL,updated_at=? WHERE id=?",&[json!(p.to_string()),json!(now()),json!(id)]).await?;
    // Job ownership and original feature permissions are validated at claim and
    // again before each request/commit; retry never grants an account a role.
    if let Some(item) = p["item_id"].as_str()
        && matches!(kind.as_str(), "import" | "import_content")
    {
        tx.execute("UPDATE items SET content_state='queued',content_error=NULL,updated_at=? WHERE id=? AND content_job_id=? AND NOT EXISTS(SELECT 1 FROM assets WHERE assets.item_id=items.id)",&[json!(now()),json!(item),json!(id)]).await?;
    }
    j = tx
        .query("SELECT * FROM jobs WHERE id=?", &[json!(id)])
        .await?
        .remove(0);
    tx.commit().await?;
    Ok(Json(queue::job_view(j)))
}
pub async fn cancel(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    let _ = queue::locked(&s, &mut tx).await?;
    let j = tx
        .query("SELECT * FROM jobs WHERE id=?", &[json!(id)])
        .await?
        .pop()
        .ok_or_else(|| ApiError::missing("任务不存在"))?;
    if !matches!(j["status"].as_str(), Some("queued" | "running")) {
        return Err(ApiError(StatusCode::CONFLICT, "该任务已结束"));
    }
    tx.execute("UPDATE jobs SET status='cancelled',worker_id=NULL,lease_until=NULL,updated_at=? WHERE id=?",&[json!(now()),json!(id)]).await?;
    let p = json_column(&j["payload"]);
    if matches!(j["kind"].as_str(), Some("import" | "import_content"))
        && let Some(item) = p["item_id"].as_str()
    {
        tx.execute("UPDATE items SET content_state='metadata',content_error=NULL,updated_at=? WHERE id=? AND content_job_id=? AND NOT EXISTS(SELECT 1 FROM assets WHERE assets.item_id=items.id)",&[json!(now()),json!(item),json!(id)]).await?;
    }
    let j = tx
        .query("SELECT * FROM jobs WHERE id=?", &[json!(id)])
        .await?
        .remove(0);
    tx.commit().await?;
    Ok(Json(queue::job_view(j)))
}
