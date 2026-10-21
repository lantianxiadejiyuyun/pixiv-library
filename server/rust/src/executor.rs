use crate::{
    AppState, accounts,
    content_store::{self, Staging, Work},
    db::{Tx, json_column, now},
    error::{ApiError, Result},
    platforms, secrets,
    tasks::{self, Context, TaskResult},
};
use serde_json::{Value, json};
use std::{collections::HashSet, future::Future, pin::Pin};
pub struct NativeExecutor;
impl tasks::Executor for NativeExecutor {
    fn execute(
        &self,
        ctx: Context,
        job: Value,
    ) -> Pin<Box<dyn Future<Output = TaskResult<Value>> + Send + 'static>> {
        Box::pin(async move {
            let p = json_column(&job["payload"]);
            match job["kind"].as_str().unwrap_or("") {
                "import" | "import_content" => import(ctx, &job, &p).await,
                "pixiv_bookmark" => bookmark(ctx, &p).await,
                "author_profile" => crate::authors::sync_profile(ctx, &p).await,
                "author_feed" => crate::authors::sync_feed(ctx, &p).await,
                "source_sync" => crate::acquisition::source_sync(ctx, &p).await,
                "series_sync" => crate::series_sync::process(ctx, &p).await,
                "ai" => crate::ai_nodes::process(ctx, &p).await,
                "translate" => crate::translation::process(ctx, &p).await,
                _ => Err(ApiError::bad("不支持的任务类型，请检查任务记录").into()),
            }
        })
    }
}
pub async fn identity(
    s: &AppState,
    p: &Value,
    field: &str,
    purpose: &str,
    retry: bool,
) -> Result<(Option<String>, Value)> {
    let platform = p["platform"].as_str().unwrap_or("pixiv");
    let requested = p[field]
        .as_str()
        .or_else(|| p["execution_account_id"].as_str())
        .or_else(|| p["account_id"].as_str());
    let id = if platform == "pixiv" {
        accounts::resolve(s, requested, purpose, p[field].is_string(), retry).await?
    } else {
        requested.map(str::to_owned)
    };
    let c = accounts::credentials(s, platform, id.as_deref()).await?;
    Ok((id, c))
}
pub async fn import(ctx: Context, job: &Value, p: &Value) -> TaskResult<Value> {
    let s = &ctx.state;
    let platform = p["platform"]
        .as_str()
        .ok_or_else(|| ApiError::bad("任务缺少平台"))?;
    let ident = platforms::normalize(platform, p["remote_id"].as_str().unwrap_or(""))?;
    let download = job["kind"] == "import_content" || p["download"] != false;
    let bundled = platform == "pixiv" && download;
    let existing =
        s.db.one(
            "SELECT * FROM items WHERE source=? AND source_id=?",
            &[json!(platform), json!(ident)],
        )
        .await?;
    if let Some(expected) = p["item_id"].as_str()
        && existing.as_ref().is_none_or(|v| v["id"] != expected)
    {
        return Ok(json!({"item_id":expected,"skipped":true,"message":"作品已删除，任务已跳过"}));
    }
    let assets = if let Some(i) = &existing {
        s.db.query(
            "SELECT * FROM assets WHERE item_id=? ORDER BY position",
            &[i["id"].clone()],
        )
        .await?
    } else {
        vec![]
    };
    let images_only = p["novel_images_only"] == true;
    if !assets.is_empty()
        && !images_only
        && (download
            || p["refresh_metadata"] != true
                && existing.as_ref().is_some_and(|i| {
                    json_column(&i["preview_cover"]).is_object()
                        || !matches!(i["type"].as_str(), Some("novel" | "article"))
                }))
    {
        return Ok(
            json!({"item_id":existing.as_ref().unwrap()["id"],"existing":true,"metadata_only":false,"content_state":"ready"}),
        );
    }
    if p["auto_novel_cover"] == true
        && existing
            .as_ref()
            .is_none_or(|i| !json_column(&i["preview_cover"]).is_null())
    {
        return Ok(
            json!({"item_id":p["item_id"],"skipped":true,"cover_saved":true,"message":"封面已存在或作品已删除，自动补全已跳过"}),
        );
    }
    let body = download && (bundled || job["kind"] == "import_content");
    let novel = accounts::is_novel(p);
    let manual = p["download_origin"] == "manual";
    let (field, purpose) = if body && platform == "pixiv" {
        if manual {
            ("download_account_id", "manual")
        } else if novel {
            ("details_account_id", "details")
        } else {
            ("download_account_id", "content")
        }
    } else {
        (
            "details_account_id",
            if platform == "pixiv" {
                "details"
            } else {
                "metadata"
            },
        )
    };
    let (body_owner, credentials) = identity(s, p, field, purpose, ctx.lane == "retry").await?;
    let split = bundled
        && !novel
        && accounts::roles(s).await?.enabled
        && p["download_account_id"] != p["details_account_id"];
    let mut metadata = None;
    let mut bookmark_owner = body_owner.clone();
    if split {
        let (owner, c) =
            identity(s, p, "details_account_id", "details", ctx.lane == "retry").await?;
        metadata =
            Some(platforms::fetch(s, platform, &c, &ident, false, false, Some(ctx.clone())).await?);
        bookmark_owner = owner;
    }
    let mut work = if images_only {
        let i = existing
            .as_ref()
            .ok_or_else(|| ApiError::bad("小说已删除"))?;
        if i["type"] != "novel" {
            return Err(ApiError::bad("仅小说支持插图补全").into());
        }
        let texts: Vec<_> = assets
            .iter()
            .filter(|a| {
                a["mime_type"]
                    .as_str()
                    .is_some_and(|v| v.starts_with("text/"))
            })
            .collect();
        if texts.len() != 1
            || texts[0]["size_bytes"].as_u64().unwrap_or(u64::MAX) > 16 * 1024 * 1024
        {
            return Err(ApiError::bad("插图补全需要一份有效小说正文").into());
        }
        let rel = texts[0]["relative_path"].as_str().unwrap_or("").to_owned();
        let root = s.config.storage_root.clone();
        let text = crate::media::io(s, move || {
            use std::io::Read;
            let path = crate::media::resolve(&root, &rel)?;
            let mut data = vec![];
            std::fs::File::open(path)?
                .take(16 * 1024 * 1024 + 1)
                .read_to_end(&mut data)?;
            if data.len() > 16 * 1024 * 1024 {
                return Err(ApiError::bad("小说正文过大"));
            }
            String::from_utf8(data).map_err(|_| ApiError::bad("小说正文不是 UTF-8"))
        })
        .await?;
        let api = platforms::pixiv::Pixiv::connect(s, &credentials, Some(ctx.clone())).await?;
        let response = api.novel_body(ident.trim_start_matches("novel:")).await?;
        let mut work = Work::new(&ident, i["title"].as_str().unwrap_or(""), "novel")?;
        let existing_keys: HashSet<_> = assets
            .iter()
            .filter(|a| a["status"] == "available")
            .filter_map(|a| a["inline_key"].as_str().map(str::to_owned))
            .collect();
        api.novel_images(&response, &text, &mut work, &existing_keys)
            .await?;
        work
    } else {
        platforms::fetch(
            s,
            platform,
            &credentials,
            &ident,
            body,
            true,
            Some(ctx.clone()),
        )
        .await?
    };
    if work.remote_id != ident || body && !images_only && work.files.entries.is_empty() {
        return Err(ApiError::bad("平台未返回匹配作品的完整正文，未保存空作品").into());
    }
    if let Some(mut m) = metadata {
        if m.remote_id != ident
            || m.kind != work.kind
            || m.page_count
                .is_some_and(|n| n != work.files.entries.len() as i64)
        {
            return Err(ApiError::bad("Pixiv 正文与作品页数不一致，未保存不完整作品").into());
        }
        m.files = work.files;
        m.preview = work.preview;
        work = m;
    }
    let files = std::mem::replace(&mut work.files, Staging::new()?);
    let preview = work.preview.take();
    let prepared = content_store::prepare(s, platform, files, preview, Some(&ctx)).await?;
    ctx.checkpoint(Some(96.0)).await?;
    let mut tx = s.db.begin().await?;
    let current = ctx.guard_tx(&mut tx).await?;
    let mut latest = json_column(&current["payload"]);
    if let Some(expected) = latest["item_id"].as_str()
        && tx
            .query("SELECT id FROM items WHERE id=?", &[json!(expected)])
            .await?
            .is_empty()
    {
        return Ok(json!({"skipped":true,"message":"作品已删除，任务已跳过"}));
    }
    let item = if images_only {
        tx.query("SELECT * FROM items WHERE id=?", &[p["item_id"].clone()])
            .await?
            .pop()
            .ok_or(crate::tasks::TaskError::Cancelled)?
    } else {
        content_store::merge(s, &mut tx, platform, &work).await?
    };
    let id = item["id"].as_str().unwrap();
    let added = content_store::attach(&mut tx, id, &prepared.assets, images_only).await?;
    if !images_only {
        if let Some(preview) = prepared.preview {
            tx.execute(
                "UPDATE items SET preview_cover=? WHERE id=?",
                &[json!(preview.to_string()), json!(id)],
            )
            .await?;
        }
        if body {
            content_store::set_tags(s, &mut tx, id, &work.tags, "source").await?;
            tx.execute("UPDATE items SET rating=?,rating_origin='source' WHERE id=? AND rating_origin!='manual' AND ?!='unknown'",&[json!(work.rating),json!(id),json!(work.rating)]).await?;
            tx.execute(
                "UPDATE items SET content_job_id=? WHERE id=?",
                &[json!(ctx.id), json!(id)],
            )
            .await?;
        }
        if platform == "pixiv" {
            observe(
                s,
                &mut tx,
                id,
                &work,
                bookmark_owner.as_deref(),
                &latest["pixiv_bookmark_state"],
            )
            .await?;
        }
    }
    latest["item_id"] = json!(id);
    tx.execute(
        "UPDATE jobs SET payload=? WHERE id=? AND worker_id=?",
        &[json!(latest.to_string()), json!(ctx.id), json!(ctx.owner)],
    )
    .await?;
    let mut content_job = Value::Null;
    if download && !body {
        let active=tx.query(&format!("SELECT * FROM jobs WHERE kind='import_content' AND status IN ('queued','running') AND {}=? ORDER BY created_at,id LIMIT 1",tasks::json_text(s,"payload","item_id")),&[json!(id)]).await?.pop();
        let j = if let Some(mut j) = active {
            let mut p = json_column(&j["payload"]);
            if j["status"] == "queued" {
                if latest["download_origin"] == "manual" {
                    p["download_origin"] = json!("manual");
                }
                if latest["retry_queue"] == true {
                    p["retry_queue"] = json!(true);
                    p["retry_last_error"] = latest["retry_last_error"].clone();
                }
                tx.execute(
                    "UPDATE jobs SET payload=? WHERE id=?",
                    &[json!(p.to_string()), j["id"].clone()],
                )
                .await?;
                j["payload"] = p;
            }
            j
        } else {
            let mut p = latest.clone();
            p["remote_id"] = json!(ident);
            p["download"] = json!(true);
            tasks::enqueue_tx(s, &mut tx, "import_content", &p).await?
        };
        content_job = j["id"].clone();
        tx.execute(
            "UPDATE items SET content_state=?,content_job_id=?,content_error=NULL WHERE id=?",
            &[
                json!(if j["status"] == "running" {
                    "downloading"
                } else {
                    "queued"
                }),
                content_job.clone(),
                json!(id),
            ],
        )
        .await?;
    }
    tx.commit().await?;
    Ok(if images_only {
        json!({"item_id":id,"illustrations_added":added,"message":"正文插图已补全，原文与阅读进度已保留"})
    } else {
        json!({"item_id":id,"metadata_only":!body,"content_state":if body{"ready"}else if download{"queued"}else{"metadata"},"content_job_id":if body{json!(ctx.id)}else{content_job},"message":if body{"完整正文已保存，可离线阅读"}else if download{"资料已保存，正文已加入独立下载队列"}else{"作品资料已保存"}})
    })
}
pub async fn main_id(s: &AppState, tx: &mut Tx) -> Result<Option<String>> {
    let roles = tasks::roles_tx(s, tx).await?;
    Ok(if roles.enabled {
        roles.main_account_id
    } else {
        tx.query(
            "SELECT id FROM source_accounts WHERE platform='pixiv' ORDER BY created_at,id LIMIT 1",
            &[],
        )
        .await?
        .pop()
        .and_then(|v| v["id"].as_str().map(str::to_owned))
    })
}
pub async fn setting_tx(s: &AppState, tx: &mut Tx, key: &str) -> Result<Value> {
    let row = tx
        .query(
            "SELECT value_encrypted FROM network_settings WHERE `key`=?",
            &[json!(key)],
        )
        .await?
        .pop();
    row.map(|v| {
        secrets::decrypt(
            &s.config.secret,
            v["value_encrypted"].as_str().unwrap_or(""),
        )
    })
    .transpose()
    .map(|v| v.unwrap_or_else(|| json!({})))
}
pub async fn add_favorite(s: &AppState, tx: &mut Tx, id: &str) -> Result<()> {
    let sql = if s.db.mysql() {
        "INSERT INTO item_favorites(item_id,created_at) VALUES(?,?) ON DUPLICATE KEY UPDATE item_id=item_id"
    } else {
        "INSERT INTO item_favorites(item_id,created_at) VALUES(?,?) ON CONFLICT(item_id) DO NOTHING"
    };
    tx.execute(sql, &[json!(id), json!(now())]).await?;
    Ok(())
}
pub async fn observe(
    s: &AppState,
    tx: &mut Tx,
    id: &str,
    w: &Work,
    owner: Option<&str>,
    hint: &Value,
) -> Result<()> {
    let Some(main) = main_id(s, tx).await? else {
        return Ok(());
    };
    let (owner, state) = if hint["account_id"] == main && hint["bookmarked"].is_boolean() {
        (Some(main.as_str()), hint["bookmarked"].as_bool())
    } else {
        (owner, w.bookmarked)
    };
    if owner == Some(main.as_str()) && state.is_some() {
        if state == Some(true) {
            add_favorite(s, tx, id).await?;
            crate::acquisition::queue_bookmark_download(s, tx, id, &main).await?;
        }
        return Ok(());
    }
    if !tx
        .query(
            "SELECT item_id FROM item_favorites WHERE item_id=?",
            &[json!(id)],
        )
        .await?
        .is_empty()
        && !crate::db::truth(
            &setting_tx(s, tx, crate::subscriptions::BOOKMARK_KEY).await?["enabled"],
        )
    {
        return Ok(());
    }
    let sql = format!(
        "SELECT id FROM jobs WHERE kind='pixiv_bookmark' AND status IN ('queued','running') AND {}=? AND {}=? LIMIT 1",
        tasks::json_text(s, "payload", "item_id"),
        tasks::json_text(s, "payload", "main_account_id")
    );
    if tx.query(&sql, &[json!(id), json!(main)]).await?.is_empty() {
        tasks::enqueue_tx(s,tx,"pixiv_bookmark",&json!({"platform":"pixiv","item_id":id,"remote_id":w.remote_id,"account_id":main,"main_account_id":main})).await?;
    }
    Ok(())
}
pub async fn bookmark(ctx: Context, p: &Value) -> TaskResult<Value> {
    let s = &ctx.state;
    let id = p["item_id"].as_str().unwrap_or("");
    let Some(item) =
        s.db.one(
            "SELECT * FROM items WHERE id=? AND source='pixiv' AND source_id=?",
            &[json!(id), p["remote_id"].clone()],
        )
        .await?
    else {
        return Ok(json!({"skipped":true,"message":"作品已移除，收藏检查已跳过"}));
    };
    let (owner, c) = identity(s, p, "check_account_id", "metadata", ctx.lane == "retry").await?;
    if owner.as_deref() != p["main_account_id"].as_str() {
        return Err(ApiError::bad("Pixiv 主账号已更改，请重新采集作品后检查收藏").into());
    }
    let api = platforms::pixiv::Pixiv::connect(s, &c, Some(ctx.clone())).await?;
    let w = api
        .fetch(p["remote_id"].as_str().unwrap_or(""), false, false)
        .await?;
    let state = w
        .bookmarked
        .ok_or_else(|| ApiError::bad("Pixiv 未返回明确收藏状态，本地收藏保持"))?;
    let mut tx = s.db.begin().await?;
    ctx.guard_tx(&mut tx).await?;
    if main_id(s, &mut tx).await?.as_deref() != owner.as_deref() {
        return Err(ApiError::bad("Pixiv 主账号已更改，未使用旧账号收藏状态").into());
    }
    if tx
        .query("SELECT id FROM items WHERE id=?", &[item["id"].clone()])
        .await?
        .is_empty()
    {
        return Ok(json!({"skipped":true,"message":"作品已移除"}));
    }
    if state {
        add_favorite(s, &mut tx, id).await?;
        crate::acquisition::queue_bookmark_download(s, &mut tx, id, owner.as_deref().unwrap())
            .await?;
    }
    let favorite = !tx
        .query(
            "SELECT item_id FROM item_favorites WHERE item_id=?",
            &[json!(id)],
        )
        .await?
        .is_empty();
    tx.commit().await?;
    Ok(
        json!({"item_id":id,"is_favorite":favorite,"message":if state{"主账号收藏已加入本地收藏"}else{"主账号未收藏，本地收藏保持"}}),
    )
}
