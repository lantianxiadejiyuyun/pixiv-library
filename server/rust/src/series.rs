use crate::{
    ApiJson, AppState,
    db::{Db, Tx, now, truth},
    error::{ApiError, Result},
    library::{cards, placeholders},
};
use axum::{
    Json,
    extract::{Path, State},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::HashSet;
pub async fn summaries(db: &Db, item: &str) -> Result<Vec<Value>> {
    let mut rows=db.query("SELECT s.id,s.title,s.origin,s.source,s.source_id,s.kind,s.complete,s.revision FROM series s JOIN series_entries e ON e.series_id=s.id WHERE e.item_id=? ORDER BY s.origin,s.title,s.id",&[json!(item)]).await?;
    for r in &mut rows {
        r["complete"] = json!(truth(&r["complete"]));
    }
    Ok(rows)
}
async fn detail(s: &AppState, id: &str) -> Result<Value> {
    let row =
        s.db.one(
            "SELECT id,title,origin,source,source_id,kind,complete,revision FROM series WHERE id=?",
            &[json!(id)],
        )
        .await?
        .ok_or_else(|| ApiError::missing("系列不存在"))?;
    let items=s.db.query("SELECT i.* FROM items i JOIN series_entries e ON e.item_id=i.id WHERE e.series_id=? ORDER BY e.position",&[json!(id)]).await?;
    let entries =
        s.db.query(
            "SELECT item_id,position,label FROM series_entries WHERE series_id=? ORDER BY position",
            &[json!(id)],
        )
        .await?;
    let views = cards(&s.db, items, false).await?;
    let mut result = row;
    result["complete"] = json!(truth(&result["complete"]));
    result["entries"] = json!(
        entries
            .into_iter()
            .zip(views)
            .map(|(mut e, v)| {
                e["title"] = v["title"].clone();
                e["content_state"] = v["content_state"].clone();
                e["page_count"] = v["page_count"].clone();
                e
            })
            .collect::<Vec<_>>()
    );
    Ok(result)
}
pub async fn get(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    Ok(Json(detail(&s, &id).await?))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    item_id: String,
    #[serde(default)]
    label: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Create {
    title: String,
    kind: String,
    entries: Vec<Entry>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Update {
    title: String,
    revision: u32,
    entries: Vec<Entry>,
}
fn validate(title: &str, entries: &[Entry]) -> Result<()> {
    if title.trim().is_empty()
        || title.chars().count() > 512
        || entries.len() > 2000
        || entries
            .iter()
            .any(|e| e.item_id.is_empty() || e.item_id.len() > 36 || e.label.chars().count() > 512)
    {
        return Err(ApiError::invalid("系列名称、作品数量或条目内容不符合要求"));
    }
    let unique: HashSet<_> = entries.iter().map(|e| &e.item_id).collect();
    if unique.len() != entries.len() {
        return Err(ApiError::bad("同一系列不能重复添加同一作品"));
    }
    Ok(())
}
async fn replace(tx: &mut Tx, id: &str, entries: &[Entry], mysql: bool) -> Result<()> {
    if !entries.is_empty() {
        let rows = tx
            .query(
                &format!(
                    "SELECT id FROM items WHERE id IN ({}) ORDER BY id{}",
                    placeholders(entries.len()),
                    if mysql { " FOR UPDATE" } else { "" }
                ),
                &entries.iter().map(|e| json!(e.item_id)).collect::<Vec<_>>(),
            )
            .await?;
        if rows.len() != entries.len() {
            return Err(ApiError::missing("部分作品已不存在，请刷新后重试"));
        }
    }
    tx.execute("DELETE FROM series_entries WHERE series_id=?", &[json!(id)])
        .await?;
    for (pos, e) in entries.iter().enumerate() {
        tx.execute(
            "INSERT INTO series_entries(series_id,item_id,position,label) VALUES(?,?,?,?)",
            &[
                json!(id),
                json!(e.item_id),
                json!(pos),
                json!(e.label.trim()),
            ],
        )
        .await?;
    }
    Ok(())
}
pub async fn create(State(s): State<AppState>, ApiJson(v): ApiJson<Create>) -> Result<Json<Value>> {
    s.config.writable()?;
    validate(&v.title, &v.entries)?;
    if v.entries.is_empty() || !matches!(v.kind.as_str(), "novel" | "manga") {
        return Err(ApiError::invalid(
            "系列至少包含一个作品，类型须为漫画或小说",
        ));
    }
    let id = uuid::Uuid::new_v4().to_string();
    let mut tx = s.db.begin().await?;
    tx.execute("INSERT INTO series(id,title,kind,origin,source,source_id,complete,revision,created_at,updated_at) VALUES(?,?,?,'manual','local',NULL,1,1,?,?)",&[json!(id),json!(v.title.trim()),json!(v.kind),json!(now()),json!(now())]).await?;
    replace(&mut tx, &id, &v.entries, s.db.mysql()).await?;
    tx.commit().await?;
    Ok(Json(detail(&s, &id).await?))
}
pub async fn update(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(v): ApiJson<Update>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    validate(&v.title, &v.entries)?;
    if v.revision == 0 {
        return Err(ApiError::invalid("系列版本号无效"));
    }
    let mut tx = s.db.begin().await?;
    let row = tx
        .query(
            &format!(
                "SELECT origin,revision FROM series WHERE id=?{}",
                if s.db.mysql() { " FOR UPDATE" } else { "" }
            ),
            &[json!(id)],
        )
        .await?
        .pop()
        .ok_or_else(|| ApiError::missing("系列不存在"))?;
    if row["origin"] != "manual" {
        return Err(ApiError(
            axum::http::StatusCode::CONFLICT,
            "官方系列按平台目录排序；请创建手工系列",
        ));
    }
    if row["revision"].as_u64() != Some(v.revision.into()) {
        return Err(ApiError(
            axum::http::StatusCode::CONFLICT,
            "系列已被其他页面修改，请刷新后重试",
        ));
    }
    replace(&mut tx, &id, &v.entries, s.db.mysql()).await?;
    tx.execute(
        "UPDATE series SET title=?,revision=revision+1,updated_at=? WHERE id=?",
        &[json!(v.title.trim()), json!(now()), json!(id)],
    )
    .await?;
    tx.commit().await?;
    Ok(Json(detail(&s, &id).await?))
}
