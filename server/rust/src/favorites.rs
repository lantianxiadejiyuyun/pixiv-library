use crate::{
    ApiJson, AppState,
    db::{Tx, now},
    error::{ApiError, Result},
    library::{placeholders, require_item},
};
use axum::{
    Json,
    extract::{Path, State},
};
use serde::Deserialize;
use serde_json::{Value, json};
use unicode_normalization::UnicodeNormalization;

pub async fn groups(State(s): State<AppState>) -> Result<Json<Value>> {
    Ok(Json(
        json!({"items":s.db.query("SELECT g.id,g.name,COUNT(l.item_id) AS count FROM favorite_groups g LEFT JOIN favorite_group_items l ON l.group_id=g.id GROUP BY g.id,g.name,g.created_at ORDER BY g.created_at,g.id",&[]).await?,"total_favorites":s.db.count("SELECT COUNT(*) FROM item_favorites",&[]).await?,"ungrouped_count":s.db.count("SELECT COUNT(*) FROM item_favorites f WHERE NOT EXISTS(SELECT 1 FROM favorite_group_items g WHERE g.item_id=f.item_id)",&[]).await?}),
    ))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Group {
    name: String,
}
fn name(raw: &str) -> Result<String> {
    let n = raw.nfc().collect::<String>().trim().to_owned();
    if n.is_empty() || n.chars().count() > 80 || n.chars().any(|c| (c as u32) < 32) {
        return Err(ApiError::invalid(
            "分组名称不能为空、超过 80 字符或包含控制字符",
        ));
    }
    Ok(n)
}
pub async fn create_group(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<Group>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let n = name(&v.name)?;
    let id = uuid::Uuid::new_v4().to_string();
    let t = now();
    s.db.execute(
        "INSERT INTO favorite_groups(id,name,created_at,updated_at) VALUES(?,?,?,?)",
        &[json!(id), json!(n), json!(t), json!(t)],
    )
    .await?;
    Ok(Json(json!({"id":id,"name":n})))
}
pub async fn rename_group(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(v): ApiJson<Group>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let n = name(&v.name)?;
    let mut tx = s.db.begin().await?;
    group_exists(&mut tx, &id, s.db.mysql()).await?;
    tx.execute(
        "UPDATE favorite_groups SET name=?,updated_at=? WHERE id=?",
        &[json!(n), json!(now()), json!(id)],
    )
    .await?;
    tx.commit().await?;
    Ok(Json(json!({"id":id,"name":n})))
}
async fn group_exists(tx: &mut Tx, id: &str, mysql: bool) -> Result<()> {
    if tx
        .query(
            &format!(
                "SELECT id FROM favorite_groups WHERE id=?{}",
                if mysql { " FOR UPDATE" } else { "" }
            ),
            &[json!(id)],
        )
        .await?
        .is_empty()
    {
        return Err(ApiError::missing("收藏分组不存在"));
    }
    Ok(())
}
pub async fn remove_group(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    group_exists(&mut tx, &id, s.db.mysql()).await?;
    tx.execute("DELETE FROM favorite_groups WHERE id=?", &[json!(id)])
        .await?;
    tx.commit().await?;
    Ok(Json(json!({"ok":true})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Favorite {
    favorite: bool,
    group_ids: Option<Vec<String>>,
    score: Option<u8>,
}
pub async fn favorite(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(v): ApiJson<Favorite>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    if v.group_ids
        .as_ref()
        .is_some_and(|v| v.len() > 200 || v.iter().any(|id| id.is_empty() || id.len() > 36))
        || v.score.is_some_and(|n| n > 5)
    {
        return Err(ApiError::invalid("收藏分组或评分超出范围"));
    }
    let mut ids = v.group_ids.clone().unwrap_or_default();
    ids.sort();
    ids.dedup();
    if !ids.is_empty() && !v.favorite {
        return Err(ApiError::bad("取消收藏时不能指定分组"));
    }
    let mut tx = s.db.begin().await?;
    if !ids.is_empty() {
        let rows = tx
            .query(
                &format!(
                    "SELECT id FROM favorite_groups WHERE id IN ({}) ORDER BY id{}",
                    placeholders(ids.len()),
                    if s.db.mysql() { " FOR UPDATE" } else { "" }
                ),
                &ids.iter().map(|id| json!(id)).collect::<Vec<_>>(),
            )
            .await?;
        if rows.len() != ids.len() {
            return Err(ApiError::bad("收藏分组已删除，请刷新后重新选择"));
        }
    }
    let item = tx
        .query(
            &format!(
                "SELECT user_score FROM items WHERE id=?{}",
                if s.db.mysql() { " FOR UPDATE" } else { "" }
            ),
            &[json!(id)],
        )
        .await?
        .pop()
        .ok_or_else(|| ApiError::missing("作品不存在"))?;
    let score = v
        .score
        .map(i64::from)
        .unwrap_or(item["user_score"].as_i64().unwrap_or(0));
    if v.score.is_some() {
        tx.execute(
            "UPDATE items SET user_score=?,updated_at=? WHERE id=?",
            &[json!(score), json!(now()), json!(id)],
        )
        .await?;
    }
    if v.favorite {
        let sql = format!(
            "INSERT INTO item_favorites(item_id,created_at) VALUES(?,?) {}",
            s.db.upsert("item_id", "item_id")
        );
        tx.execute(&sql, &[json!(id), json!(now())]).await?;
        if v.group_ids.is_some() {
            tx.execute(
                "DELETE FROM favorite_group_items WHERE item_id=?",
                &[json!(id)],
            )
            .await?;
            for gid in &ids {
                tx.execute(
                    "INSERT INTO favorite_group_items(group_id,item_id) VALUES(?,?)",
                    &[json!(gid), json!(id)],
                )
                .await?;
            }
        } else {
            ids = tx
                .query(
                    "SELECT group_id FROM favorite_group_items WHERE item_id=? ORDER BY group_id",
                    &[json!(id)],
                )
                .await?
                .into_iter()
                .filter_map(|v| v["group_id"].as_str().map(str::to_owned))
                .collect();
        }
    } else {
        tx.execute("DELETE FROM item_favorites WHERE item_id=?", &[json!(id)])
            .await?;
    }
    tx.commit().await?;
    Ok(Json(
        json!({"id":id,"is_favorite":v.favorite,"favorite_group_ids":ids,"user_score":score}),
    ))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Score {
    score: u8,
}
pub async fn score(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(v): ApiJson<Score>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    if v.score > 5 {
        return Err(ApiError::invalid("评分必须在 0–5 之间"));
    }
    require_item(&s.db, &id).await?;
    s.db.execute(
        "UPDATE items SET user_score=?,updated_at=? WHERE id=?",
        &[json!(v.score), json!(now()), json!(id)],
    )
    .await?;
    Ok(Json(json!({"id":id,"user_score":v.score})))
}
pub async fn visit(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    s.config.writable()?;
    require_item(&s.db, &id).await?;
    let sql = format!(
        "INSERT INTO item_visits(item_id,viewed_at) VALUES(?,?) {}",
        s.db.upsert("viewed_at", "item_id")
    );
    s.db.execute(&sql, &[json!(id), json!(now())]).await?;
    Ok(Json(json!({"ok":true})))
}
pub async fn clear_history(State(s): State<AppState>) -> Result<Json<Value>> {
    s.config.writable()?;
    s.db.execute("DELETE FROM item_visits", &[]).await?;
    Ok(Json(json!({"ok":true})))
}
