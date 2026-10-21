use crate::{
    ApiJson, AppState, accounts,
    db::{Tx, iso, now, truth},
    error::{ApiError, Result},
    executor, platforms, queue, secrets, tasks,
};
use axum::{
    Json,
    extract::{Path, State},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
pub const FOLLOW_KEY: &str = "pixiv_followed_author_download";
pub const BOOKMARK_KEY: &str = "pixiv_own_bookmark_download";
pub fn ready(p: &str) -> bool {
    ["pixiv", "picacg", "kemono"].contains(&p)
}
pub async fn validate_account(s: &AppState, p: &str, id: Option<&str>) -> Result<()> {
    if let Some(id) = id
        && s.db
            .one(
                "SELECT id FROM source_accounts WHERE id=? AND platform=?",
                &[json!(id), json!(p)],
            )
            .await?
            .is_none()
    {
        return Err(ApiError::bad("账号不存在或与平台不匹配"));
    }
    Ok(())
}
fn platform(p: &str) -> Result<()> {
    if accounts::PLATFORMS.iter().any(|(name, _)| *name == p) {
        Ok(())
    } else {
        Err(ApiError::missing("未知平台"))
    }
}
pub fn flags(row: &Value, default: bool) -> (bool, bool) {
    let old = truth(&row["auto_download"]);
    (
        row["download_works"]
            .as_bool()
            .or_else(|| row["download_works"].as_i64().map(|v| v != 0))
            .unwrap_or(
                old || default && row["platform"] == "pixiv" && row["target_type"] == "author",
            ),
        if row["download_bookmarks"].is_null() {
            old
        } else {
            truth(&row["download_bookmarks"])
        },
    )
}
pub fn view(row: Value, default: bool) -> Value {
    let (works, bookmarks) = flags(&row, default);
    let mut v = row.clone();
    for key in [
        "created_at",
        "updated_at",
        "last_checked_at",
        "next_check_at",
    ] {
        v[key] = iso(&row[key]);
    }
    v["enabled"] = json!(truth(&row["enabled"]));
    v["auto_download"] = json!(
        if row["platform"] == "pixiv" && row["target_type"] == "author" {
            works || bookmarks
        } else {
            truth(&row["auto_download"])
        }
    );
    v["download_works"] = json!(works);
    v["download_bookmarks"] = json!(bookmarks);
    let r = ready(row["platform"].as_str().unwrap_or(""));
    v["runtime_status"] = json!(if !truth(&row["enabled"]) {
        "paused"
    } else if r {
        "active"
    } else {
        "pending"
    });
    v["status_message"] = json!(if !r {
        "规则已保存，平台定时检查待实现"
    } else if row["platform"] == "pixiv" && row["target_type"] == "author" {
        "检查作者作品及公开收藏的最新一页；不扩展收藏作品的作者；历史补全待实现"
    } else {
        "定时检查最新一页；历史补全待实现"
    });
    v
}
pub async fn list(State(s): State<AppState>) -> Result<Json<Value>> {
    let default = secrets::get(&s, FOLLOW_KEY)
        .await?
        .is_some_and(|v| truth(&v["enabled"]));
    Ok(Json(
        json!({"items":s.db.query("SELECT * FROM subscriptions ORDER BY created_at DESC,id",&[]).await?.into_iter().map(|v|view(v,default)).collect::<Vec<_>>()}),
    ))
}
pub async fn tags(State(s): State<AppState>) -> Result<Json<Value>> {
    Ok(Json(
        json!({"items":s.db.query("SELECT * FROM subscriptions WHERE target_type='tag' ORDER BY created_at DESC,id",&[]).await?.into_iter().map(|v|view(v,false)).collect::<Vec<_>>()}),
    ))
}
#[derive(Deserialize, Serialize, Default)]
#[serde(default)]
pub struct Input {
    pub platform: String,
    pub target_type: String,
    pub target: String,
    pub label: String,
    pub enabled: Option<bool>,
    pub auto_download: Option<bool>,
    pub download_works: Option<bool>,
    pub download_bookmarks: Option<bool>,
    pub content_scope: Option<String>,
    pub account_id: Option<String>,
    pub interval_minutes: Option<i64>,
}
pub fn validate(row: &Value) -> Result<()> {
    let p = row["platform"].as_str().unwrap_or("");
    platform(p)?;
    let kind = row["target_type"].as_str().unwrap_or("");
    let target = row["target"].as_str().unwrap_or("");
    if !["author", "tag", "search"].contains(&kind)
        || target.trim().is_empty()
        || target.chars().count() > 512
        || row["label"].as_str().unwrap_or("").chars().count() > 191
        || !row["interval_minutes"]
            .as_i64()
            .is_some_and(|v| (30..=10080).contains(&v))
    {
        return Err(ApiError::invalid("订阅目标、名称或检查间隔无效"));
    }
    let scope = row["content_scope"].as_str().unwrap_or("");
    if !["images", "novels", "all"].contains(&scope) || p != "pixiv" && scope != "images" {
        return Err(ApiError::bad("当前仅 Pixiv 支持小说订阅范围"));
    }
    if kind == "author" {
        if p == "pixiv" {
            platforms::positive(target)?;
        }
        if p == "picacg" {
            return Err(ApiError::bad("哔咔作者订阅暂不支持，请使用标签或搜索"));
        }
        if p == "kemono" {
            platforms::kemono::normalize(target, true)?;
        }
    } else if p == "kemono"
        && (target.chars().count() > 200 || kind == "search" && target.trim().chars().count() < 3)
    {
        return Err(ApiError::bad(
            "Kemono 搜索至少 3 个字符，标签与搜索最多 200 个字符",
        ));
    }
    Ok(())
}
fn initial(v: &Value) -> Value {
    let p = v["platform"].as_str().unwrap_or("");
    let author = p == "pixiv" && v["target_type"] == "author";
    let mut row = json!({"id":uuid::Uuid::new_v4().to_string(),"platform":p,"target_type":v["target_type"],"target":v["target"],"label":v["label"].as_str().map(str::trim).filter(|s|!s.is_empty()).map(str::to_owned).unwrap_or_else(||v["target"].as_str().unwrap_or("").chars().take(191).collect()),"enabled":v["enabled"].as_bool().unwrap_or(true),"auto_download":v["auto_download"].as_bool().unwrap_or(false),"download_works":v["download_works"],"download_bookmarks":v["download_bookmarks"],"content_scope":v["content_scope"].as_str().unwrap_or(if author{"all"}else{"images"}),"account_id":v["account_id"],"interval_minutes":v["interval_minutes"].as_i64().unwrap_or(360),"last_checked_at":null,"next_check_at":null,"last_error":if ready(p){Value::Null}else{json!("该平台定时检查待实现；规则已保存")},"created_at":now(),"updated_at":now()});
    if v.get("auto_download").is_some()
        && v.get("download_works").is_none()
        && v.get("download_bookmarks").is_none()
    {
        for key in ["download_works", "download_bookmarks"] {
            row[key] = if v["auto_download"] == true {
                Value::Null
            } else {
                json!(false)
            };
        }
    }
    row
}
pub async fn insert(tx: &mut Tx, row: &Value) -> Result<()> {
    let cols = [
        "id",
        "platform",
        "target_type",
        "target",
        "label",
        "enabled",
        "auto_download",
        "download_works",
        "download_bookmarks",
        "content_scope",
        "account_id",
        "interval_minutes",
        "last_checked_at",
        "next_check_at",
        "last_error",
        "created_at",
        "updated_at",
    ];
    let args = cols.iter().map(|k| row[*k].clone()).collect::<Vec<_>>();
    tx.execute(
        &format!(
            "INSERT INTO subscriptions({}) VALUES({})",
            cols.join(","),
            vec!["?"; cols.len()].join(",")
        ),
        &args,
    )
    .await?;
    Ok(())
}
pub async fn create(
    State(s): State<AppState>,
    ApiJson(value): ApiJson<Value>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let _: Input =
        serde_json::from_value(value.clone()).map_err(|_| ApiError::invalid("订阅字段类型无效"))?;
    let row = initial(&value);
    validate(&row)?;
    validate_account(
        &s,
        row["platform"].as_str().unwrap(),
        row["account_id"].as_str(),
    )
    .await?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    insert(&mut tx, &row).await?;
    let default = truth(&executor::setting_tx(&s, &mut tx, FOLLOW_KEY).await?["enabled"]);
    tx.commit().await?;
    Ok(Json(view(row, default)))
}
pub async fn patch(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(changes): ApiJson<Value>,
) -> Result<Json<Value>> {
    patch_inner(s, id, changes, false).await.map(Json)
}
pub async fn tag_patch(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(changes): ApiJson<Value>,
) -> Result<Json<Value>> {
    if changes.as_object().is_none_or(|v| {
        v.keys().any(|k| {
            ![
                "enabled",
                "auto_download",
                "content_scope",
                "interval_minutes",
            ]
            .contains(&k.as_str())
        })
    }) {
        return Err(ApiError::invalid("标签追踪字段无效"));
    }
    patch_inner(s, id, changes, true).await.map(Json)
}
async fn patch_inner(s: AppState, id: String, changes: Value, tag_only: bool) -> Result<Value> {
    s.config.writable()?;
    let _: Input = serde_json::from_value(changes.clone())
        .map_err(|_| ApiError::invalid("订阅字段类型无效"))?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let mut row = tx
        .query("SELECT * FROM subscriptions WHERE id=?", &[json!(id)])
        .await?
        .pop()
        .filter(|v| !tag_only || v["target_type"] == "tag")
        .ok_or_else(|| ApiError::missing("订阅不存在"))?;
    let cols = [
        "target",
        "label",
        "enabled",
        "auto_download",
        "download_works",
        "download_bookmarks",
        "content_scope",
        "account_id",
        "interval_minutes",
    ];
    for key in cols {
        if let Some(v) = changes.get(key)
            && (!v.is_null()
                || ["account_id", "download_works", "download_bookmarks"].contains(&key))
        {
            row[key] = v.clone();
        }
    }
    if changes.get("auto_download").is_some_and(|v| v.is_boolean())
        && changes.get("download_works").is_none()
        && changes.get("download_bookmarks").is_none()
    {
        for key in ["download_works", "download_bookmarks"] {
            row[key] = if changes["auto_download"] == true {
                Value::Null
            } else {
                json!(false)
            };
        }
    }
    validate(&row)?;
    if let Some(account) = row["account_id"].as_str()
        && tx
            .query(
                "SELECT id FROM source_accounts WHERE id=? AND platform=?",
                &[json!(account), row["platform"].clone()],
            )
            .await?
            .is_empty()
    {
        return Err(ApiError::bad("账号不存在或与平台不匹配"));
    }
    row["next_check_at"] = Value::Null;
    row["updated_at"] = json!(now());
    let mut args = cols.iter().map(|k| row[*k].clone()).collect::<Vec<_>>();
    args.extend([row["updated_at"].clone(), json!(id)]);
    tx.execute(
        &format!(
            "UPDATE subscriptions SET {},next_check_at=NULL,updated_at=? WHERE id=?",
            cols.iter()
                .map(|k| format!("{k}=?"))
                .collect::<Vec<_>>()
                .join(",")
        ),
        &args,
    )
    .await?;
    let default = truth(&executor::setting_tx(&s, &mut tx, FOLLOW_KEY).await?["enabled"]);
    tx.commit().await?;
    Ok(view(row, default))
}
pub async fn remove(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    remove_inner(s, id, false).await.map(Json)
}
pub async fn tag_remove(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    remove_inner(s, id, true).await.map(Json)
}
async fn remove_inner(s: AppState, id: String, tag: bool) -> Result<Value> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let count = tx
        .execute(
            if tag {
                "DELETE FROM subscriptions WHERE id=? AND target_type='tag'"
            } else {
                "DELETE FROM subscriptions WHERE id=?"
            },
            &[json!(id)],
        )
        .await?;
    if count == 0 {
        return Err(ApiError::missing("订阅不存在"));
    }
    tx.commit().await?;
    Ok(json!({"ok":true}))
}
pub async fn enqueue_check(s: &AppState, tx: &mut Tx, row: &Value) -> Result<Value> {
    let active=tx.query(&format!("SELECT * FROM jobs WHERE kind='source_sync' AND status IN ('queued','running') AND {}=? ORDER BY created_at,id LIMIT 1",tasks::json_text(s,"payload","subscription_id")),&[row["id"].clone()]).await?.pop();
    if let Some(j) = active {
        return Ok(j);
    }
    tasks::enqueue_tx(s,tx,"source_sync",&json!({"platform":row["platform"],"subscription_id":row["id"],"account_id":row["account_id"]})).await
}
pub async fn tag_check(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let row = tx
        .query(
            "SELECT * FROM subscriptions WHERE id=? AND target_type='tag'",
            &[json!(id)],
        )
        .await?
        .pop()
        .ok_or_else(|| ApiError::missing("标签追踪不存在"))?;
    if !truth(&row["enabled"]) {
        return Err(ApiError(
            axum::http::StatusCode::CONFLICT,
            "请先恢复标签追踪",
        ));
    }
    if !ready(row["platform"].as_str().unwrap_or("")) {
        return Err(ApiError::bad("该平台尚未支持标签追踪"));
    }
    let j = enqueue_check(&s, &mut tx, &row).await?;
    tx.commit().await?;
    Ok(Json(queue::job_view(j)))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tracking {
    #[serde(default = "pixiv")]
    platform: String,
    tags: Vec<String>,
    #[serde(default = "yes")]
    auto_download: bool,
    #[serde(default = "all")]
    content_scope: String,
    #[serde(default = "interval")]
    interval_minutes: i64,
}
fn pixiv() -> String {
    "pixiv".into()
}
fn all() -> String {
    "all".into()
}
fn yes() -> bool {
    true
}
fn interval() -> i64 {
    360
}
pub async fn add_tags(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<Tracking>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    if !ready(&v.platform) || v.tags.is_empty() || v.tags.len() > 20 {
        return Err(ApiError::invalid("选择支持的平台及 1–20 个标签"));
    }
    let tags = crate::content_store::tags(&v.tags)?;
    if tags.is_empty() {
        return Err(ApiError::bad("请填写至少一个标签"));
    }
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let mut items = vec![];
    let mut jobs = vec![];
    for tag in tags {
        let old=tx.query("SELECT * FROM subscriptions WHERE platform=? AND target_type='tag' AND target=? ORDER BY created_at,id LIMIT 1",&[json!(v.platform),json!(tag)]).await?.pop();
        let mut row = old.clone().unwrap_or_else(|| {
            initial(&json!({"platform":v.platform,"target_type":"tag","target":tag,"label":tag}))
        });
        row["enabled"] = json!(true);
        row["auto_download"] = json!(v.auto_download);
        row["interval_minutes"] = json!(v.interval_minutes);
        row["content_scope"] = json!(if v.platform == "pixiv" {
            v.content_scope.as_str()
        } else {
            "images"
        });
        validate(&row)?;
        if old.is_none() {
            insert(&mut tx, &row).await?;
        } else {
            tx.execute("UPDATE subscriptions SET enabled=1,auto_download=?,content_scope=?,interval_minutes=?,updated_at=? WHERE id=?",&[json!(v.auto_download),row["content_scope"].clone(),json!(v.interval_minutes),json!(now()),row["id"].clone()]).await?;
        }
        jobs.push(enqueue_check(&s, &mut tx, &row).await?["id"].clone());
        items.push(view(row, false));
    }
    tx.commit().await?;
    Ok(Json(json!({"items":items,"job_ids":jobs})))
}
pub async fn sources(State(s): State<AppState>) -> Result<Json<Value>> {
    let rows =
        s.db.query(
            "SELECT id,platform,label,verified_at FROM source_accounts ORDER BY created_at,id",
            &[],
        )
        .await?;
    let mut result = vec![];
    for (p, label) in accounts::PLATFORMS {
        let capabilities=[("import","内容导入",true),("sync","关注导入",["pixiv","picacg"].contains(&p)),("subscriptions","定时订阅",ready(p))].into_iter().map(|(key,label,ready)|json!({"key":key,"label":label,"status":if ready{"available"}else{"unavailable"}})).collect::<Vec<_>>();
        let accounts=rows.iter().filter(|r|r["platform"]==p).map(|r|json!({"id":r["id"],"label":r["label"],"auth_status":if r["verified_at"].is_null(){"unverified"}else{"verified"},"verified_at":iso(&r["verified_at"])})).collect::<Vec<_>>();
        result.push(json!({"platform":p,"label":label,"status":"ready","capabilities":capabilities,"accounts":accounts,"message":if p=="pixiv"{"同步主账号关注，检查作者作品与公开收藏，不沿收藏作品扩展作者"}else{"支持独立书库导入与离线保存"}}));
    }
    Ok(Json(json!(result)))
}
#[derive(Deserialize, Default)]
#[serde(default)]
pub struct Import {
    url: Option<String>,
    remote_id: Option<String>,
    account_id: Option<String>,
}
pub async fn source_import(
    State(s): State<AppState>,
    Path(platform): Path<String>,
    ApiJson(v): ApiJson<Import>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    self::platform(&platform)?;
    let input = v
        .url
        .as_deref()
        .filter(|s| !s.is_empty())
        .or(v.remote_id.as_deref())
        .unwrap_or("");
    if input.len() > 2048 {
        return Err(ApiError::invalid("来源链接过长"));
    }
    let id = platforms::normalize(&platform, input)?;
    validate_account(&s, &platform, v.account_id.as_deref()).await?;
    if platform == "pixiv" {
        let owner = accounts::resolve(&s, v.account_id.as_deref(), "manual", false, false).await?;
        accounts::validate_credentials(
            &platform,
            &accounts::credentials(&s, &platform, owner.as_deref()).await?,
        )?;
    } else if platform == "picacg" {
        accounts::validate_credentials(
            &platform,
            &accounts::credentials(&s, &platform, v.account_id.as_deref()).await?,
        )?;
    }
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let j=tasks::enqueue_import_tx(&s,&mut tx,json!({"platform":platform,"remote_id":id,"account_id":v.account_id,"download_origin":"manual"})).await?;
    tx.commit().await?;
    Ok(Json(queue::job_view(j)))
}
#[derive(Deserialize, Default)]
#[serde(default)]
pub struct Sync {
    account_id: Option<String>,
}
pub async fn download(
    State(s): State<AppState>,
    Path(id): Path<String>,
    value: Option<Json<Sync>>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let item = crate::library::require_item(&s.db, &id).await?;
    let p = item["source"].as_str().unwrap_or("");
    if p == "local" || item["source_id"].is_null() {
        return Err(ApiError::bad("本地作品没有可重新获取的远端来源"));
    }
    platform(p)?;
    if s.db
        .count("SELECT COUNT(*) FROM assets WHERE item_id=?", &[json!(id)])
        .await?
        > 0
    {
        return Err(ApiError(
            axum::http::StatusCode::CONFLICT,
            "正文已经保存，缺失文件请检查存储或恢复备份",
        ));
    }
    let account = value.and_then(|Json(v)| v.account_id);
    validate_account(&s, p, account.as_deref()).await?;
    if p == "pixiv" {
        accounts::resolve(&s, account.as_deref(), "manual", false, false).await?;
    }
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let j=tasks::enqueue_import_tx(&s,&mut tx,json!({"platform":p,"remote_id":item["source_id"],"item_id":id,"account_id":account,"download":true,"download_origin":"manual"})).await?;
    tx.commit().await?;
    Ok(Json(queue::job_view(j)))
}
pub async fn source_sync(
    State(s): State<AppState>,
    Path(platform): Path<String>,
    ApiJson(v): ApiJson<Sync>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    if !["pixiv", "picacg"].contains(&platform.as_str()) {
        return Err(ApiError(
            axum::http::StatusCode::NOT_IMPLEMENTED,
            "此平台关注同步待实现",
        ));
    }
    validate_account(&s, &platform, v.account_id.as_deref()).await?;
    let id = if platform == "pixiv" {
        accounts::resolve(&s, v.account_id.as_deref(), "metadata", false, false).await?
    } else {
        v.account_id
    };
    accounts::validate_credentials(
        &platform,
        &accounts::credentials(&s, &platform, id.as_deref()).await?,
    )?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let j = tasks::enqueue_tx(
        &s,
        &mut tx,
        "source_sync",
        &json!({"platform":platform,"account_id":id}),
    )
    .await?;
    tx.commit().await?;
    Ok(Json(queue::job_view(j)))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Toggle {
    pub enabled: bool,
}
async fn get_policy(s: &AppState, key: &str, scope: &str) -> Result<Value> {
    Ok(
        json!({"enabled":secrets::get(s,key).await?.is_some_and(|v|truth(&v["enabled"])),"scope":scope}),
    )
}
async fn set_policy(s: &AppState, key: &str, scope: &str, v: Toggle) -> Result<Value> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    queue::locked(s, &mut tx).await?;
    let mut old = executor::setting_tx(s, &mut tx, key).await?;
    let previous = truth(&old["enabled"]);
    old["enabled"] = json!(v.enabled);
    secrets::put_tx(s, &mut tx, key, &old).await?;
    if key == FOLLOW_KEY && v.enabled && !previous {
        tx.execute("UPDATE subscriptions SET next_check_at=NULL WHERE platform='pixiv' AND target_type='author' AND enabled=1 AND download_works IS NULL AND auto_download=0",&[]).await?;
    }
    tx.commit().await?;
    Ok(json!({"enabled":v.enabled,"scope":scope}))
}
pub async fn follow_policy(State(s): State<AppState>) -> Result<Json<Value>> {
    get_policy(&s, FOLLOW_KEY, "subscribed_author_works")
        .await
        .map(Json)
}
pub async fn set_follow_policy(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<Toggle>,
) -> Result<Json<Value>> {
    set_policy(&s, FOLLOW_KEY, "subscribed_author_works", v)
        .await
        .map(Json)
}
pub async fn bookmark_policy(State(s): State<AppState>) -> Result<Json<Value>> {
    get_policy(&s, BOOKMARK_KEY, "main_account_bookmarks")
        .await
        .map(Json)
}
pub async fn set_bookmark_policy(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<Toggle>,
) -> Result<Json<Value>> {
    set_policy(&s, BOOKMARK_KEY, "main_account_bookmarks", v)
        .await
        .map(Json)
}
