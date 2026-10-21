use crate::{
    AppState,
    db::{Db, iso, json_column, truth},
    error::{ApiError, Result},
};
use axum::{
    Json,
    extract::{Path, Query, State},
};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};

pub type Params = HashMap<String, String>;
pub fn text<'a>(q: &'a Params, k: &str) -> &'a str {
    q.get(k).map(String::as_str).unwrap_or("")
}
pub fn number(q: &Params, k: &str, default: i64, min: i64, max: i64) -> Result<i64> {
    let n = if let Some(s) = q.get(k) {
        s.parse()
            .map_err(|_| ApiError::invalid("分页参数必须是整数"))?
    } else {
        default
    };
    if n < min || n > max {
        Err(ApiError::invalid("分页参数超出范围"))
    } else {
        Ok(n)
    }
}
pub fn flag(q: &Params, k: &str) -> Result<bool> {
    match text(q, k) {
        "" | "false" | "0" | "False" => Ok(false),
        "true" | "1" | "True" => Ok(true),
        _ => Err(ApiError::invalid("筛选条件必须是布尔值")),
    }
}
pub fn like(s: &str) -> String {
    s.replace('!', "!!").replace('%', "!%").replace('_', "!_")
}
pub fn placeholders(n: usize) -> String {
    std::iter::repeat_n("?", n).collect::<Vec<_>>().join(",")
}
pub const DOWNLOADED: &str = "EXISTS (SELECT 1 FROM assets a WHERE a.item_id=i.id) AND NOT EXISTS (SELECT 1 FROM assets a WHERE a.item_id=i.id AND a.status<>'available') AND (i.type NOT IN ('manga','illustration') OR i.remote_page_count IS NULL OR i.remote_page_count<=0 OR (SELECT COUNT(*) FROM assets a WHERE a.item_id=i.id)>=i.remote_page_count)";
pub async fn require_item(db: &Db, id: &str) -> Result<Value> {
    db.one("SELECT * FROM items WHERE id=?", &[json!(id)])
        .await?
        .ok_or_else(|| ApiError::missing("作品不存在"))
}
pub async fn list(State(state): State<AppState>, Query(q): Query<Params>) -> Result<Json<Value>> {
    let page = number(&q, "page", 1, 1, 1_000_000)?;
    let size = number(&q, "page_size", 30, 1, 100)?;
    let mut filters = vec!["1=1".to_owned()];
    let mut args = Vec::new();
    if text(&q, "q").chars().count() > 512 || text(&q, "author").chars().count() > 512 {
        return Err(ApiError::invalid("搜索文字过长"));
    }
    let term = text(&q, "q").trim();
    if !term.is_empty() {
        filters.push("(LOWER(i.title) LIKE LOWER(?) ESCAPE '!' OR (i.title_zh_source=i.title AND LOWER(i.title_zh) LIKE LOWER(?) ESCAPE '!') OR LOWER(i.author) LIKE LOWER(?) ESCAPE '!' OR LOWER(i.description) LIKE LOWER(?) ESCAPE '!' OR EXISTS(SELECT 1 FROM item_tags it JOIN tags t ON t.id=it.tag_id WHERE it.item_id=i.id AND (LOWER(t.name) LIKE LOWER(?) ESCAPE '!' OR LOWER(t.name_zh) LIKE LOWER(?) ESCAPE '!')))".to_owned());
        args.extend(std::iter::repeat_n(json!(format!("%{}%", like(term))), 6));
    }
    for key in ["type", "source", "rating", "author"] {
        if !text(&q, key).is_empty() {
            filters.push(format!("i.{key}=?"));
            args.push(json!(text(&q, key)));
        }
    }
    if !text(&q, "tag").is_empty() {
        filters.push("EXISTS(SELECT 1 FROM item_tags it JOIN tags t ON t.id=it.tag_id WHERE it.item_id=i.id AND (t.id=? OR t.name=?))".to_owned());
        args.extend([json!(text(&q, "tag")), json!(text(&q, "tag"))]);
    }
    let mut ids = Vec::new();
    let mut seen = HashSet::new();
    for id in text(&q, "tag_ids")
        .split(',')
        .map(str::trim)
        .filter(|v| !v.is_empty())
    {
        if id.len() > 36 {
            return Err(ApiError::invalid("标签 ID 过长"));
        }
        if seen.insert(id) {
            ids.push(id);
        }
    }
    if ids.len() > 20 {
        return Err(ApiError::invalid("最多选择 20 个标签"));
    }
    let mode = text(&q, "tag_mode");
    if !matches!(mode, "" | "all" | "any") {
        return Err(ApiError::invalid("标签匹配方式无效"));
    }
    if !ids.is_empty() {
        if mode == "any" {
            filters.push(format!(
                "EXISTS(SELECT 1 FROM item_tags it WHERE it.item_id=i.id AND it.tag_id IN ({}))",
                placeholders(ids.len())
            ));
            args.extend(ids.iter().map(|id| json!(id)));
        } else {
            for id in ids {
                filters.push(
                    "EXISTS(SELECT 1 FROM item_tags it WHERE it.item_id=i.id AND it.tag_id=?)"
                        .to_owned(),
                );
                args.push(json!(id));
            }
        }
    }
    if flag(&q, "downloaded")? {
        filters.push(DOWNLOADED.to_owned());
    }
    let group = text(&q, "favorite_group");
    if flag(&q, "favorite")? || !group.is_empty() {
        filters.push("EXISTS(SELECT 1 FROM item_favorites f WHERE f.item_id=i.id)".to_owned());
        if group == "ungrouped" {
            filters.push(
                "NOT EXISTS(SELECT 1 FROM favorite_group_items g WHERE g.item_id=i.id)".to_owned(),
            );
        } else if !group.is_empty() {
            if state
                .db
                .count(
                    "SELECT COUNT(*) FROM favorite_groups WHERE id=?",
                    &[json!(group)],
                )
                .await?
                == 0
            {
                return Err(ApiError::missing("收藏分组不存在"));
            }
            filters.push("EXISTS(SELECT 1 FROM favorite_group_items g WHERE g.item_id=i.id AND g.group_id=?)".to_owned());
            args.push(json!(group));
        }
    }
    let history = flag(&q, "history")?;
    let from = if history {
        "items i JOIN item_visits v ON v.item_id=i.id"
    } else {
        "items i"
    };
    let clause = filters.join(" AND ");
    let total = state
        .db
        .count(
            &format!("SELECT COUNT(*) FROM {from} WHERE {clause}"),
            &args,
        )
        .await?;
    let order = if history {
        "v.viewed_at DESC"
    } else {
        match text(&q, "sort") {
            "title" => "i.title ASC",
            "oldest" => "i.created_at ASC",
            _ => "i.created_at DESC",
        }
    };
    args.extend([json!(size), json!((page - 1) * size)]);
    let rows = state
        .db
        .query(
            &format!(
                "SELECT i.* FROM {from} WHERE {clause} ORDER BY {order},i.id LIMIT ? OFFSET ?"
            ),
            &args,
        )
        .await?;
    Ok(Json(
        json!({"items":cards(&state.db,rows,false).await?,"total":total,"page":page,"page_size":size}),
    ))
}
pub async fn detail(State(state): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    let item = require_item(&state.db, &id).await?;
    let mut view = cards(&state.db, vec![item], true).await?.remove(0);
    view["series"] = json!(crate::series::summaries(&state.db, &id).await?);
    if view["source"] == "pixiv"
        && view["author_source_id"].is_null()
        && view["author"].as_str().is_some_and(|v| !v.is_empty())
    {
        let candidates=state.db.query("SELECT DISTINCT target FROM subscriptions WHERE platform='pixiv' AND target_type='author' AND label=? LIMIT 2",&[view["author"].clone()]).await?;
        if candidates.len() == 1 {
            view["author_source_id"] = candidates[0]["target"].clone();
        }
    }
    Ok(Json(view))
}
pub async fn cards(db: &Db, items: Vec<Value>, detail: bool) -> Result<Vec<Value>> {
    if items.is_empty() {
        return Ok(vec![]);
    }
    let ids: Vec<_> = items.iter().map(|i| i["id"].clone()).collect();
    let marks = placeholders(ids.len());
    let totals=db.query(&format!("SELECT item_id,SUM(CASE WHEN inline_key IS NULL THEN 1 ELSE 0 END) AS page_count,SUM(size_bytes) AS size_bytes,MAX(CASE WHEN status<>'available' THEN 1 ELSE 0 END) AS missing,MIN(CASE WHEN mime_type LIKE 'image/%' AND status='available' THEN position ELSE NULL END) AS first_image FROM assets WHERE item_id IN ({marks}) GROUP BY item_id"),&ids).await?;
    let totals: HashMap<_, _> = totals
        .into_iter()
        .map(|v| (v["item_id"].as_str().unwrap().to_owned(), v))
        .collect();
    let tag_rows=db.query(&format!("SELECT it.item_id,t.id,t.name,t.name_zh,it.origin,it.score FROM item_tags it JOIN tags t ON t.id=it.tag_id WHERE it.item_id IN ({marks}) ORDER BY it.item_id,it.tag_id,it.origin"),&ids).await?;
    let mut tags: HashMap<String, Vec<Value>> = HashMap::new();
    for mut t in tag_rows {
        let id = t["item_id"].as_str().unwrap().to_owned();
        t.as_object_mut().unwrap().remove("item_id");
        t["display_name"] = if t["name_zh"].as_str().is_some_and(|s| !s.is_empty()) {
            t["name_zh"].clone()
        } else {
            t["name"].clone()
        };
        tags.entry(id).or_default().push(t);
    }
    let mut progress: HashMap<String, Value> = HashMap::new();
    for mut p in db.query(&format!("SELECT item_id,page,section,offset FROM reading_progress WHERE item_id IN ({marks})"),&ids).await?{let id=p["item_id"].as_str().unwrap().to_owned();p.as_object_mut().unwrap().remove("item_id");progress.insert(id,p);}
    let fav_rows=db.query(&format!("SELECT f.item_id,g.group_id FROM item_favorites f LEFT JOIN favorite_group_items g ON f.item_id=g.item_id WHERE f.item_id IN ({marks}) ORDER BY g.group_id"),&ids).await?;
    let mut favorites: HashMap<String, Vec<Value>> = HashMap::new();
    for f in fav_rows {
        let groups = favorites
            .entry(f["item_id"].as_str().unwrap().to_owned())
            .or_default();
        if !f["group_id"].is_null() {
            groups.push(f["group_id"].clone());
        }
    }
    let mut covers: HashMap<String, Value> = HashMap::new();
    let mut cover_clauses = Vec::new();
    let mut cover_args = Vec::new();
    for (id, t) in &totals {
        if !t["first_image"].is_null() {
            cover_clauses.push("(item_id=? AND position=?)");
            cover_args.extend([json!(id), t["first_image"].clone()]);
        }
    }
    if !cover_clauses.is_empty() {
        for a in db
            .query(
                &format!(
                    "SELECT item_id,id FROM assets WHERE {}",
                    cover_clauses.join(" OR ")
                ),
                &cover_args,
            )
            .await?
        {
            covers.insert(
                a["item_id"].as_str().unwrap().to_owned(),
                json!(format!(
                    "/api/assets/{}/thumbnail",
                    a["id"].as_str().unwrap()
                )),
            );
        }
    }
    let all_assets = if detail {
        db.query(
            &format!("SELECT * FROM assets WHERE item_id IN ({marks}) ORDER BY position,id"),
            &ids,
        )
        .await?
    } else {
        vec![]
    };
    let mut out = Vec::new();
    for item in items {
        let id = item["id"].as_str().unwrap();
        let t = totals.get(id);
        let count = t.and_then(|t| t["page_count"].as_i64()).unwrap_or(0);
        let missing = t.is_some_and(|t| truth(&t["missing"]));
        let mut v = json!({});
        for k in [
            "id",
            "title",
            "type",
            "source",
            "source_url",
            "description",
            "author",
            "language",
            "rating",
            "rating_origin",
            "content_error",
            "content_job_id",
            "remote_page_count",
            "source_bookmark_count",
            "author_source_id",
            "source_id",
        ] {
            v[k] = item[k].clone();
        }
        v["created_at"] = iso(&item["created_at"]);
        v["updated_at"] = iso(&item["updated_at"]);
        v["source_published_at"] = iso(&item["source_published_at"]);
        v["source_series"] = json_column(&item["source_series"]);
        v["title_zh"] = if item["title_zh_source"] == item["title"] {
            item["title_zh"].clone()
        } else {
            Value::Null
        };
        v["display_title"] = if v["title_zh"].as_str().is_some_and(|s| !s.is_empty()) {
            v["title_zh"].clone()
        } else {
            item["title"].clone()
        };
        v["tags"] = json!(tags.remove(id).unwrap_or_default());
        v["progress"] = progress.remove(id).unwrap_or(Value::Null);
        v["content_state"] = if count > 0 {
            json!(if missing { "failed" } else { "ready" })
        } else {
            item["content_state"].clone()
        };
        if missing {
            v["content_error"] = json!("正文文件缺失，请检查 NAS 或恢复备份");
        }
        v["page_count"] = json!(if count == 0
            && !matches!(item["type"].as_str(), Some("novel" | "article"))
        {
            item["remote_page_count"]
                .as_i64()
                .filter(|n| *n > 0)
                .unwrap_or(count)
        } else {
            count
        });
        v["size_bytes"] = json!(t.and_then(|t| t["size_bytes"].as_i64()).unwrap_or(0));
        v["cover_url"] = if !json_column(&item["preview_cover"]).is_null() {
            json!(format!("/api/items/{id}/cover"))
        } else {
            covers.remove(id).unwrap_or(Value::Null)
        };
        v["is_favorite"] = json!(favorites.contains_key(id));
        v["favorite_group_ids"] = json!(favorites.remove(id).unwrap_or_default());
        v["user_score"] = json!(item["user_score"].as_i64().unwrap_or(0));
        if detail {
            let mut assets = vec![];
            let mut inline = vec![];
            for a in all_assets.iter().filter(|a| a["item_id"] == item["id"]) {
                let mut entry = json!({});
                for k in [
                    "id",
                    "position",
                    "original_name",
                    "mime_type",
                    "size_bytes",
                    "status",
                    "inline_key",
                    "width",
                    "height",
                ] {
                    entry[k] = a[k].clone();
                }
                entry["url"] = json!(format!("/api/assets/{}/content", a["id"].as_str().unwrap()));
                if a["inline_key"].is_null() {
                    assets.push(entry);
                } else {
                    inline.push(entry);
                }
            }
            v["assets"] = json!(assets);
            v["inline_assets"] = json!(inline);
        }
        out.push(v);
    }
    Ok(out)
}
pub async fn dashboard(State(state): State<AppState>) -> Result<Json<Value>> {
    let db = &state.db;
    let mut sources = json!({});
    let mut total = 0;
    for row in db
        .query(
            "SELECT source,COUNT(*) AS n FROM items GROUP BY source",
            &[],
        )
        .await?
    {
        total += row["n"].as_i64().unwrap_or(0);
        sources[row["source"].as_str().unwrap()] = row["n"].clone();
    }
    let size = db
        .one(
            "SELECT COUNT(*) AS n,COALESCE(SUM(size_bytes),0) AS bytes FROM assets",
            &[],
        )
        .await?
        .unwrap();
    let mut ratings = json!({});
    for row in db
        .query(
            "SELECT rating,COUNT(*) AS n FROM items GROUP BY rating",
            &[],
        )
        .await?
    {
        ratings[row["rating"].as_str().unwrap()] = row["n"].clone();
    }
    let tagged = db
        .count("SELECT COUNT(DISTINCT item_id) FROM item_tags", &[])
        .await?;
    let recent = db
        .query("SELECT * FROM items ORDER BY created_at DESC LIMIT 8", &[])
        .await?;
    Ok(Json(
        json!({"total_items":total,"total_pages":size["n"],"total_bytes":size["bytes"],"pending_jobs":db.count("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')",&[]).await?,"untagged_items":total-tagged,"recent_items":cards(db,recent,false).await?,"source_counts":sources,"rating_counts":ratings}),
    ))
}
pub async fn tags(State(state): State<AppState>, Query(q): Query<Params>) -> Result<Json<Value>> {
    let term = text(&q, "q").trim();
    if term.chars().count() > 191 {
        return Err(ApiError::invalid("标签搜索文字过长"));
    }
    let suggest = flag(&q, "suggest")?;
    let limit = if suggest {
        Some(number(&q, "limit", 12, 1, 1000)?.min(20))
    } else if q.contains_key("limit") {
        Some(number(&q, "limit", 1000, 1, 1000)?)
    } else {
        None
    };
    let offset = number(&q, "offset", 0, 0, 1_000_000)?;
    let mut args = vec![];
    let source = text(&q, "source");
    let source_condition = if source.is_empty() {
        ""
    } else {
        args.push(json!(source));
        " AND i.source=?"
    };
    let mut sql = format!(
        "SELECT t.id,t.name,t.name_zh,(SELECT COUNT(DISTINCT it.item_id) FROM item_tags it JOIN items i ON i.id=it.item_id WHERE it.tag_id=t.id{source_condition}) AS count FROM tags t WHERE (SELECT 1 FROM item_tags it JOIN items i ON i.id=it.item_id WHERE it.tag_id=t.id{source_condition} LIMIT 1) IS NOT NULL"
    );
    if !source.is_empty() {
        args.push(json!(source));
    }
    if !term.is_empty() {
        sql.push_str(" AND (LOWER(t.name) LIKE LOWER(?) ESCAPE '!' OR LOWER(t.name_zh) LIKE LOWER(?) ESCAPE '!')");
        args.extend([
            json!(format!("%{}%", like(term))),
            json!(format!("%{}%", like(term))),
        ]);
    }
    if suggest && !term.is_empty() {
        sql.push_str(" ORDER BY CASE WHEN LOWER(t.name)=LOWER(?) OR LOWER(t.name_zh)=LOWER(?) THEN 0 WHEN LOWER(t.name) LIKE LOWER(?) ESCAPE '!' OR LOWER(t.name_zh) LIKE LOWER(?) ESCAPE '!' THEN 1 ELSE 2 END,t.name,t.id");
        args.extend([
            json!(term),
            json!(term),
            json!(format!("{}%", like(term))),
            json!(format!("{}%", like(term))),
        ]);
    } else {
        sql.push_str(" ORDER BY t.name,t.id");
    }
    sql.push_str(" LIMIT ? OFFSET ?");
    args.extend([json!(limit.unwrap_or(i64::MAX)), json!(offset)]);
    let mut rows = state.db.query(&sql, &args).await?;
    for t in &mut rows {
        t["display_name"] = if t["name_zh"].as_str().is_some_and(|s| !s.is_empty()) {
            t["name_zh"].clone()
        } else {
            t["name"].clone()
        };
    }
    Ok(Json(json!(rows)))
}
pub async fn collections(State(state): State<AppState>) -> Result<Json<Value>> {
    let mut values = vec![];
    for (source, name) in [
        ("pixiv", "Pixiv"),
        ("ehentai", "E-Hentai"),
        ("picacg", "哔咔 PicACG"),
        ("jm", "JM 天堂"),
        ("kemono", "Kemono"),
        ("local", "本地书库"),
    ] {
        let count = state
            .db
            .count(
                "SELECT COUNT(*) FROM items WHERE source=?",
                &[json!(source)],
            )
            .await?;
        let saved=state.db.count("SELECT COUNT(*) FROM items i WHERE source=? AND EXISTS(SELECT 1 FROM assets a WHERE a.item_id=i.id)",&[json!(source)]).await?;
        let size=state.db.one("SELECT COUNT(*) AS n,COALESCE(SUM(a.size_bytes),0) AS bytes,COALESCE(SUM(CASE WHEN a.relative_path LIKE ? THEN 0 ELSE 1 END),0) AS legacy FROM assets a JOIN items i ON i.id=a.item_id WHERE i.source=?",&[json!(format!("collections/{source}/blobs/%")),json!(source)]).await?.unwrap();
        values.push(json!({"source":source,"name":name,"storage_directory":format!("collections/{source}"),"item_count":count,"saved_item_count":saved,"metadata_item_count":count-saved,"page_count":size["n"],"size_bytes":size["bytes"],"legacy_asset_count":size["legacy"]}));
    }
    Ok(Json(json!(values)))
}
pub async fn settings(State(state): State<AppState>) -> Result<Json<Value>> {
    let ai_mode = if crate::ai_nodes::image_target(&state).await?.is_some() {
        "独立 AI 服务"
    } else {
        "unavailable"
    };
    Ok(Json(
        json!({"storage_root":state.config.storage_root,"database_kind":if state.db.mysql(){"mysql"}else{"sqlite"},"storage_available":crate::media::storage_ok(&state).await,"ai_mode":ai_mode,"import_root":state.config.import_root}),
    ))
}
