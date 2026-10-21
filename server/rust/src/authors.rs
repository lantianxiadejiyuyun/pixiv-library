use crate::{
    ApiJson, AppState, accounts, content_store,
    db::{Tx, iso, json_column, now, truth},
    error::{ApiError, Result},
    executor,
    library::{self, Params, number, text},
    platforms, queue, subscriptions, tasks,
};
use axum::{
    Json,
    body::Body,
    extract::{Path, Query, State},
    http::Request,
    response::Response,
};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
fn conflict(message: &'static str) -> ApiError {
    ApiError(axum::http::StatusCode::CONFLICT, message)
}
async fn account(s: &AppState, requested: Option<&str>, required: bool) -> Result<Option<String>> {
    let id = accounts::resolve(s, requested, "metadata", false, false).await?;
    if let Some(id) = &id {
        if s.db
            .one(
                "SELECT id FROM source_accounts WHERE id=? AND platform='pixiv'",
                &[json!(id)],
            )
            .await?
            .is_none()
        {
            return Err(conflict("Pixiv 账号不存在，请重新配置"));
        }
    } else if required {
        return Err(conflict("请先在内容来源中登录 Pixiv 账号"));
    }
    Ok(id)
}
pub async fn paused(s: &AppState) -> Result<bool> {
    let row =
        s.db.one("SELECT * FROM queue_control WHERE id=1", &[])
            .await?
            .unwrap_or(Value::Null);
    let lanes = json_column(&row["lanes"]);
    Ok(truth(&row["paused"])
        || truth(&lanes["checks"]["paused"])
        || !queue::source_enabled(&json!({"lanes":lanes}), "pixiv"))
}
pub fn scope(author: &str, section: &str, category: &str, restrict: &str, account: &str) -> String {
    let text = format!(
        "[{}]",
        [author, section, category, restrict, account]
            .iter()
            .map(|v| serde_json::to_string(v).unwrap())
            .collect::<Vec<_>>()
            .join(", ")
    );
    format!("{:x}", Sha256::digest(text.as_bytes()))
}
pub fn job_view(j: &Value) -> Value {
    let result = json_column(&j["result"]);
    json!({"id":j["id"],"status":j["status"],"progress":j["progress"],"error":j["error"],"author_id":result["author_id"],"feed_id":result["feed_id"]})
}
pub async fn enqueue(
    s: &AppState,
    tx: &mut Tx,
    kind: &str,
    p: &Value,
    identity: &str,
) -> Result<Value> {
    let existing=tx.query(&format!("SELECT * FROM jobs WHERE kind=? AND status IN ('queued','running') AND {}=? ORDER BY created_at,id LIMIT 1",tasks::json_text(s,"payload","identity")),&[json!(kind),json!(identity)]).await?.pop();
    if let Some(j) = existing {
        return Ok(j);
    }
    let count=tx.query("SELECT COUNT(*) AS n FROM jobs WHERE kind IN ('author_profile','author_feed') AND status IN ('queued','running')",&[]).await?[0]["n"].as_i64().unwrap_or(0);
    if count >= 100 {
        return Err(ApiError(
            axum::http::StatusCode::TOO_MANY_REQUESTS,
            "作者目录任务过多，请等待队列处理",
        ));
    }
    let mut p = p.clone();
    p["identity"] = json!(identity);
    tasks::enqueue_tx(s, tx, kind, &p).await
}
pub async fn list(State(s): State<AppState>, Query(q): Query<Params>) -> Result<Json<Value>> {
    let page = number(&q, "page", 1, 1, 1_000_000)?;
    let query = text(&q, "q").trim();
    if query.chars().count() > 200 {
        return Err(ApiError::invalid("搜索内容过长"));
    }
    let union = "SELECT p.remote_id,COALESCE(NULLIF(p.name,''),l.name,'') AS name FROM author_profiles p LEFT JOIN (SELECT target AS remote_id,MAX(label) AS name FROM subscriptions WHERE platform='pixiv' AND target_type='author' GROUP BY target) l ON l.remote_id=p.remote_id UNION ALL SELECT target AS remote_id,MAX(label) AS name FROM subscriptions WHERE platform='pixiv' AND target_type='author' AND NOT EXISTS(SELECT 1 FROM author_profiles p WHERE p.remote_id=subscriptions.target) GROUP BY target";
    let condition = if query.is_empty() {
        "1=1"
    } else {
        "(LOWER(name) LIKE LOWER(?) ESCAPE '!' OR remote_id LIKE ? ESCAPE '!')"
    };
    let args = if query.is_empty() {
        vec![]
    } else {
        vec![json!(format!("%{}%", library::like(query))); 2]
    };
    let total =
        s.db.count(
            &format!("SELECT COUNT(*) FROM({union}) catalog WHERE {condition}"),
            &args,
        )
        .await?;
    let mut args = args;
    args.push(json!((page - 1) * 30));
    let rows=s.db.query(&format!("SELECT * FROM({union}) catalog WHERE {condition} ORDER BY name,remote_id LIMIT 30 OFFSET ?"),&args).await?;
    Ok(Json(json!({"items":rows,"total":total,"page":page})))
}
pub async fn downloads(s: &AppState, id: &str) -> Result<Value> {
    let row=s.db.one("SELECT * FROM subscriptions WHERE platform='pixiv' AND target_type='author' AND target=? ORDER BY created_at,id LIMIT 1",&[json!(id)]).await?;
    Ok(if let Some(row) = row {
        let default = crate::secrets::get(s, subscriptions::FOLLOW_KEY)
            .await?
            .is_some_and(|v| truth(&v["enabled"]));
        let (w, b) = subscriptions::flags(&row, default);
        json!({"download_works":w,"download_bookmarks":b,"subscribed":true})
    } else {
        json!({"download_works":false,"download_bookmarks":false,"subscribed":false})
    })
}
pub async fn detail_value(s: &AppState, id: &str, requested: Option<&str>) -> Result<Value> {
    platforms::positive(id)?;
    let profile =
        s.db.one(
            "SELECT * FROM author_profiles WHERE remote_id=?",
            &[json!(id)],
        )
        .await?;
    let selected = account(s, requested, false).await?;
    let stats = profile
        .as_ref()
        .map(|p| json_column(&p["statistics"]))
        .unwrap_or_else(|| json!({}));
    let mut counts = json!({});
    for row in s.db.query("SELECT type,COUNT(*) AS n FROM items WHERE source='pixiv' AND author_source_id=? GROUP BY type",&[json!(id)]).await?{counts[row["type"].as_str().unwrap_or("")]=row["n"].clone();}
    let mut name = profile
        .as_ref()
        .and_then(|p| p["name"].as_str())
        .filter(|v| !v.is_empty())
        .map(str::to_owned);
    if name.is_none() {
        name=s.db.one("SELECT label FROM subscriptions WHERE platform='pixiv' AND target_type='author' AND target=? AND label!='' ORDER BY created_at,id LIMIT 1",&[json!(id)]).await?.and_then(|v|v["label"].as_str().map(str::to_owned));
    }
    if name.is_none() && !counts.as_object().unwrap().is_empty() {
        name=s.db.one("SELECT author FROM items WHERE source='pixiv' AND author_source_id=? AND author!='' ORDER BY created_at DESC,id LIMIT 1",&[json!(id)]).await?.and_then(|v|v["author"].as_str().map(str::to_owned));
    }
    let cached = name.is_some()
        || !counts.as_object().unwrap().is_empty()
        || profile.as_ref().is_some_and(|p| !p["synced_at"].is_null());
    let owners = stats["_owned_accounts"].as_array();
    let private = selected.is_some_and(|id| owners.is_some_and(|o| o.contains(&json!(id))));
    let stats = stats
        .as_object()
        .map(|v| {
            v.iter()
                .filter(|(k, _)| !k.starts_with('_'))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect::<serde_json::Map<_, _>>()
        })
        .unwrap_or_default();
    Ok(
        json!({"remote_id":id,"name":name.unwrap_or_else(||format!("Pixiv 作者 {id}")),"description":profile.as_ref().map(|p|p["description"].clone()).unwrap_or(json!("")),"source_url":format!("https://www.pixiv.net/users/{id}"),"avatar_url":if profile.as_ref().is_some_and(|p|!json_column(&p["avatar"]).is_null()){json!(format!("/api/pixiv/authors/{id}/avatar"))}else{Value::Null},"synced_at":profile.as_ref().map(|p|iso(&p["synced_at"])).unwrap_or(Value::Null),"statistics":stats,"can_read_private":private,"download_settings":downloads(s,id).await?,"local_counts":counts,"cached":cached,"refresh_paused":paused(s).await?}),
    )
}
pub async fn detail(
    State(s): State<AppState>,
    Path(id): Path<String>,
    Query(q): Query<Params>,
) -> Result<Json<Value>> {
    detail_value(&s, &id, q.get("account_id").map(String::as_str))
        .await
        .map(Json)
}
#[derive(Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Feed {
    pub section: String,
    pub category: String,
    pub restrict: String,
    pub account_id: Option<String>,
    pub more: bool,
}
impl Default for Feed {
    fn default() -> Self {
        Self {
            section: "works".into(),
            category: "illustration".into(),
            restrict: "public".into(),
            account_id: None,
            more: false,
        }
    }
}
impl Feed {
    fn validate(&self) -> Result<()> {
        if !["works", "bookmarks"].contains(&self.section.as_str())
            || !["illustration", "manga", "novel"].contains(&self.category.as_str())
            || !["public", "private"].contains(&self.restrict.as_str())
            || self.section == "works" && self.restrict != "public"
        {
            return Err(ApiError::bad("作者目录类型无效"));
        }
        Ok(())
    }
}
pub async fn feed_value(s: &AppState, id: &str, v: &Feed, page: i64) -> Result<Value> {
    platforms::positive(id)?;
    v.validate()?;
    let selected = account(s, v.account_id.as_deref(), false).await?;
    let feed = if let Some(account) = selected {
        s.db.one(
            "SELECT * FROM author_feeds WHERE scope_key=?",
            &[json!(scope(
                id,
                &v.section,
                &v.category,
                &v.restrict,
                &account
            ))],
        )
        .await?
    } else {
        None
    };
    let j = if let Some(f) = &feed {
        s.db.one(&format!("SELECT * FROM jobs WHERE kind='author_feed' AND {}=? ORDER BY (status IN ('queued','running')) DESC,updated_at DESC,id DESC LIMIT 1",tasks::json_text(s,"payload","feed_id")),&[f["id"].clone()]).await?
    } else {
        None
    };
    let local = feed
        .as_ref()
        .is_none_or(|f| f["generation"].as_i64().unwrap_or(0) == 0);
    let (rows, total) = if local {
        if v.section == "works" && v.restrict == "public" {
            let args = [json!(id), json!(v.category)];
            let total=s.db.count("SELECT COUNT(*) FROM items WHERE source='pixiv' AND author_source_id=? AND type=?",&args).await?;
            let rows=s.db.query("SELECT * FROM items WHERE source='pixiv' AND author_source_id=? AND type=? ORDER BY created_at DESC,id LIMIT 24 OFFSET ?",&[args[0].clone(),args[1].clone(),json!((page-1)*24)]).await?;
            (rows, total)
        } else {
            (vec![], 0)
        }
    } else {
        let f = feed.as_ref().unwrap();
        let total =
            s.db.count(
                "SELECT COUNT(*) FROM author_feed_entries WHERE feed_id=?",
                &[f["id"].clone()],
            )
            .await?;
        let rows=s.db.query("SELECT i.* FROM items i JOIN author_feed_entries e ON e.item_id=i.id WHERE e.feed_id=? ORDER BY e.generation DESC,e.position,i.id LIMIT 24 OFFSET ?",&[f["id"].clone(),json!((page-1)*24)]).await?;
        (rows, total)
    };
    let f = feed.unwrap_or(Value::Null);
    Ok(
        json!({"items":library::cards(&s.db,rows,false).await?,"total":total,"page":page,"has_more":local||!json_column(&f["next_cursor"]).is_null(),"complete":!local&&truth(&f["complete"]),"revision":if local{0}else{f["revision"].as_i64().unwrap_or(0)},"generation":if local{0}else{f["generation"].as_i64().unwrap_or(0)},"unavailable":f["unavailable"].as_i64().unwrap_or(0),"job":j.as_ref().map(job_view),"local_only":local,"refresh_paused":paused(s).await?}),
    )
}
pub async fn get_feed(
    State(s): State<AppState>,
    Path(id): Path<String>,
    Query(q): Query<Params>,
) -> Result<Json<Value>> {
    let v = Feed {
        section: q.get("section").cloned().unwrap_or_else(|| "works".into()),
        category: q
            .get("category")
            .cloned()
            .unwrap_or_else(|| "illustration".into()),
        restrict: q
            .get("restrict")
            .cloned()
            .unwrap_or_else(|| "public".into()),
        account_id: q.get("account_id").cloned(),
        more: false,
    };
    feed_value(&s, &id, &v, number(&q, "page", 1, 1, 1_000_000)?)
        .await
        .map(Json)
}
pub async fn request_feed(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(v): ApiJson<Feed>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    platforms::positive(&id)?;
    v.validate()?;
    let selected = account(&s, v.account_id.as_deref(), true).await?.unwrap();
    let key = scope(&id, &v.section, &v.category, &v.restrict, &selected);
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let profile = tx
        .query(
            "SELECT * FROM author_profiles WHERE remote_id=?",
            &[json!(id)],
        )
        .await?
        .pop();
    if v.restrict == "private"
        && !profile.as_ref().is_some_and(|p| {
            json_column(&p["statistics"])["_owned_accounts"]
                .as_array()
                .is_some_and(|v| v.contains(&json!(selected)))
        })
    {
        return Err(ApiError(
            axum::http::StatusCode::FORBIDDEN,
            "请先读取本人主页，只能访问当前账号自己的私密收藏",
        ));
    }
    if profile.is_none() {
        tx.execute("INSERT INTO author_profiles(remote_id,name,description,created_at,updated_at) VALUES(?,'','',?,?)",&[json!(id),json!(now()),json!(now())]).await?;
    }
    let feed = if let Some(f) = tx
        .query(
            "SELECT * FROM author_feeds WHERE scope_key=?",
            &[json!(key)],
        )
        .await?
        .pop()
    {
        f
    } else {
        let fid = uuid::Uuid::new_v4().to_string();
        tx.execute("INSERT INTO author_feeds(id,scope_key,author_id,account_id,section,category,`restrict`,generation,revision,seen_cursors,complete,unavailable,raw_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,0,0,'[]',0,0,0,?,?)",&[json!(fid),json!(key),json!(id),json!(selected),json!(v.section),json!(v.category),json!(v.restrict),json!(now()),json!(now())]).await?;
        tx.query("SELECT * FROM author_feeds WHERE id=?", &[json!(fid)])
            .await?
            .remove(0)
    };
    if v.more
        && feed["generation"].as_i64().unwrap_or(0) > 0
        && json_column(&feed["next_cursor"]).is_null()
    {
        tx.commit().await?;
        return Ok(Json(json!({"job":null,"cached":true})));
    }
    let job=enqueue(&s,&mut tx,"author_feed",&json!({"feed_id":feed["id"],"account_id":selected,"more":v.more,"revision":feed["revision"]}),feed["id"].as_str().unwrap()).await?;
    tx.commit().await?;
    Ok(Json(json!({"job":job_view(&job),"cached":false})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Open {
    url_or_id: String,
    #[serde(default)]
    account_id: Option<String>,
    #[serde(default)]
    prefer_cached: bool,
    #[serde(default)]
    section: Option<String>,
    #[serde(default)]
    category: Option<String>,
}
pub async fn open(State(s): State<AppState>, ApiJson(v): ApiJson<Open>) -> Result<Json<Value>> {
    if v.url_or_id.len() > 2048 {
        return Err(ApiError::invalid("作者链接过长"));
    }
    let mut t = platforms::pixiv_catalog::target(&v.url_or_id)?;
    if let Some(section) = v.section {
        t.section = section;
    }
    if let Some(category) = &v.category {
        t.category = category.clone();
    }
    let feed = Feed {
        section: t.section.clone(),
        category: t.category.clone(),
        restrict: t.restrict.clone(),
        account_id: v.account_id.clone(),
        more: false,
    };
    feed.validate()?;
    if v.prefer_cached {
        let selected = account(&s, v.account_id.as_deref(), false).await?;
        if t.id == "me"
            && let Some(selected) = selected
        {
            let pattern = json!(format!("%{}%", library::like(&selected)));
            let rows=s.db.query("SELECT remote_id,statistics FROM author_profiles WHERE synced_at IS NOT NULL AND statistics LIKE ? ESCAPE '!'",&[pattern]).await?;
            let owned: Vec<_> = rows
                .iter()
                .filter(|p| {
                    json_column(&p["statistics"])["_owned_accounts"]
                        .as_array()
                        .is_some_and(|v| v.contains(&json!(selected)))
                })
                .collect();
            if owned.len() == 1 {
                t.id = owned[0]["remote_id"].as_str().unwrap().into();
            }
        }
        let mut out = t.value();
        if t.id == "me" {
            for key in ["profile", "feed", "job"] {
                out[key] = Value::Null;
            }
            out["cached"] = json!(false);
        } else {
            let profile = detail_value(&s, &t.id, v.account_id.as_deref()).await?;
            if v.category.is_none()
                && t.section == "works"
                && t.category == "illustration"
                && profile["local_counts"]["illustration"]
                    .as_i64()
                    .unwrap_or(0)
                    == 0
            {
                for category in ["manga", "novel"] {
                    if profile["local_counts"][category].as_i64().unwrap_or(0) > 0 {
                        t.category = category.into();
                        break;
                    }
                }
            }
            let mut feed = feed;
            feed.category = t.category.clone();
            out = t.value();
            out["feed"] = feed_value(&s, &t.id, &feed, 1).await?;
            out["cached"] = profile["cached"].clone();
            out["profile"] = profile;
            out["job"] = Value::Null;
        }
        out["refresh_paused"] = json!(paused(&s).await?);
        return Ok(Json(out));
    }
    s.config.writable()?;
    let selected = account(&s, v.account_id.as_deref(), true).await?.unwrap();
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let job = enqueue(
        &s,
        &mut tx,
        "author_profile",
        &json!({"target":t.id,"account_id":selected}),
        &format!("{selected}:{}", t.id),
    )
    .await?;
    tx.commit().await?;
    let mut out = t.value();
    out["job"] = job_view(&job);
    out["refresh_paused"] = json!(paused(&s).await?);
    Ok(Json(out))
}
pub async fn task(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    let j =
        s.db.one(
            "SELECT * FROM jobs WHERE id=? AND kind IN ('author_profile','author_feed')",
            &[json!(id)],
        )
        .await?
        .ok_or_else(|| ApiError::missing("作者任务不存在"))?;
    let mut v = job_view(&j);
    v["refresh_paused"] = json!(paused(&s).await?);
    Ok(Json(v))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Downloads {
    download_works: bool,
    download_bookmarks: bool,
}
pub async fn set_downloads(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(v): ApiJson<Downloads>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    platforms::positive(&id)?;
    let selected = account(&s, None, true).await?.unwrap();
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let rows=tx.query("SELECT * FROM subscriptions WHERE platform='pixiv' AND target_type='author' AND target=?",&[json!(id)]).await?;
    if rows.is_empty() {
        let label = tx
            .query(
                "SELECT name FROM author_profiles WHERE remote_id=?",
                &[json!(id)],
            )
            .await?
            .pop()
            .and_then(|v| v["name"].as_str().map(str::to_owned))
            .unwrap_or_else(|| format!("Pixiv 作者 {id}"));
        let row = json!({"id":uuid::Uuid::new_v4().to_string(),"platform":"pixiv","target_type":"author","target":id,"label":label,"enabled":true,"auto_download":false,"download_works":v.download_works,"download_bookmarks":v.download_bookmarks,"content_scope":"all","account_id":selected,"interval_minutes":360,"last_checked_at":null,"next_check_at":null,"last_error":null,"created_at":now(),"updated_at":now()});
        subscriptions::insert(&mut tx, &row).await?;
    } else {
        tx.execute("UPDATE subscriptions SET download_works=?,download_bookmarks=?,next_check_at=CASE WHEN enabled=1 THEN NULL ELSE next_check_at END,updated_at=? WHERE platform='pixiv' AND target_type='author' AND target=?",&[json!(v.download_works),json!(v.download_bookmarks),json!(now()),json!(id)]).await?;
    }
    tx.commit().await?;
    downloads(&s, &id).await.map(Json)
}
pub async fn avatar(
    State(s): State<AppState>,
    Path(id): Path<String>,
    request: Request<Body>,
) -> Result<Response> {
    platforms::positive(&id)?;
    let p =
        s.db.one(
            "SELECT avatar FROM author_profiles WHERE remote_id=?",
            &[json!(id)],
        )
        .await?
        .ok_or_else(|| ApiError::missing("作者头像尚未保存"))?;
    crate::media::blob(&s, &json_column(&p["avatar"]), request).await
}
pub async fn sync_profile(ctx: tasks::Context, p: &Value) -> tasks::TaskResult<Value> {
    let s = &ctx.state;
    let target = p["target"].as_str().unwrap_or("");
    let (owner, c) = executor::identity(
        s,
        p,
        "check_account_id",
        if target == "me" { "metadata" } else { "checks" },
        ctx.lane == "retry",
    )
    .await?;
    let api = platforms::pixiv::Pixiv::connect(s, &c, Some(ctx.clone())).await?;
    let data = api.profile(target).await?;
    let preview = platforms::preview(
        s,
        data["avatar_url"].as_str().unwrap_or(""),
        &["pximg.net"],
        platforms::pixiv::image_headers(),
        Some(ctx.clone()),
    )
    .await?;
    let saved = content_store::prepare(
        s,
        "pixiv",
        content_store::Staging::new()?,
        preview,
        Some(&ctx),
    )
    .await?;
    let mut tx = s.db.begin().await?;
    ctx.guard_tx(&mut tx).await?;
    if tx
        .query(
            "SELECT id FROM source_accounts WHERE id=? AND platform='pixiv'",
            &[p["account_id"].clone()],
        )
        .await?
        .is_empty()
    {
        return Err(ApiError::bad("Pixiv 账号已删除，资料未保存").into());
    }
    let ident = data["remote_id"].as_str().unwrap();
    let old = tx
        .query(
            "SELECT * FROM author_profiles WHERE remote_id=?",
            &[json!(ident)],
        )
        .await?
        .pop();
    let mut stats = data["statistics"].clone();
    let mut owners = json_column(
        &old.as_ref()
            .map(|v| v["statistics"].clone())
            .unwrap_or(Value::Null),
    )["_owned_accounts"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    if owner.as_deref() == p["account_id"].as_str() {
        owners.retain(|v| v != &p["account_id"]);
        if data["is_self"] == true {
            owners.push(p["account_id"].clone());
        }
    }
    owners.sort_by(|a, b| a.as_str().cmp(&b.as_str()));
    stats["_owned_accounts"] = json!(owners);
    let avatar = saved.preview.or_else(|| {
        old.as_ref()
            .map(|v| json_column(&v["avatar"]))
            .filter(|v| !v.is_null())
    });
    let sql = format!(
        "INSERT INTO author_profiles(remote_id,name,description,avatar,statistics,synced_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) {}",
        s.db.upsert(
            "name,description,avatar,statistics,synced_at,updated_at",
            "remote_id"
        )
    );
    tx.execute(
        &sql,
        &[
            json!(ident),
            data["name"].clone(),
            data["description"].clone(),
            avatar.map(|v| json!(v.to_string())).unwrap_or(Value::Null),
            json!(stats.to_string()),
            json!(now()),
            json!(now()),
            json!(now()),
        ],
    )
    .await?;
    tx.commit().await?;
    Ok(json!({"author_id":ident,"message":"作者资料已保存"}))
}
pub fn cursor_marker(value: &Value) -> String {
    let text = if let Some(object) = value.as_object() {
        format!(
            "{{{}}}",
            object
                .iter()
                .map(|(k, v)| format!("{}: {}", serde_json::to_string(k).unwrap(), v))
                .collect::<Vec<_>>()
                .join(", ")
        )
    } else {
        value.to_string()
    };
    format!("{:x}", Sha256::digest(text))
}
pub async fn sync_feed(ctx: tasks::Context, p: &Value) -> tasks::TaskResult<Value> {
    let s = &ctx.state;
    let fid = p["feed_id"].as_str().unwrap_or("");
    let old =
        s.db.one("SELECT * FROM author_feeds WHERE id=?", &[json!(fid)])
            .await?
            .ok_or_else(|| ApiError::bad("作者目录或账号已被删除"))?;
    if old["revision"] != p["revision"] {
        return Err(ApiError::bad("作者目录已更新，请重新加载").into());
    }
    let ident = old["author_id"].as_str().unwrap();
    let section = old["section"].as_str().unwrap();
    let category = old["category"].as_str().unwrap();
    let restrict = old["restrict"].as_str().unwrap();
    let more = p["more"] == true && old["generation"].as_i64().unwrap_or(0) > 0;
    accounts::resolve(
        s,
        p["account_id"].as_str(),
        "metadata",
        true,
        ctx.lane == "retry",
    )
    .await?;
    let (owner, c) =
        executor::identity(s, p, "check_account_id", "metadata", ctx.lane == "retry").await?;
    if restrict == "private" && owner.as_deref() != p["account_id"].as_str() {
        return Err(ApiError::bad("私密收藏必须由原账号读取，请重试任务").into());
    }
    let api = platforms::pixiv::Pixiv::connect(s, &c, Some(ctx.clone())).await?;
    let known=s.db.query("SELECT i.source_id,i.id FROM items i JOIN author_feed_entries e ON e.item_id=i.id WHERE e.feed_id=? LIMIT 150001",&[json!(fid)]).await?;
    if known.len() > 150000 {
        return Err(ApiError::bad("作者目录超过 150000 项单次刷新限制").into());
    }
    let mut cursor = if more {
        json_column(&old["next_cursor"])
    } else {
        Value::Null
    };
    let mut history = if more {
        json_column(&old["seen_cursors"])
            .as_array()
            .cloned()
            .unwrap_or_default()
    } else {
        vec![]
    };
    let mut raw_count = if more {
        old["raw_count"].as_i64().unwrap_or(0)
    } else {
        0
    };
    let mut unavailable = if more {
        old["unavailable"].as_i64().unwrap_or(0)
    } else {
        0
    };
    let generation = old["generation"].as_i64().unwrap_or(0) + i64::from(!more);
    let mut works = vec![];
    for _ in 0..3 {
        let page = api
            .author_page(
                ident,
                section,
                category,
                restrict,
                if cursor.is_null() {
                    None
                } else {
                    Some(&cursor)
                },
            )
            .await?;
        raw_count += page.raw_count as i64;
        unavailable += page.unavailable as i64;
        works.extend(page.works);
        cursor = page.next.unwrap_or(Value::Null);
        if !cursor.is_null() {
            let marker = json!(cursor_marker(&cursor));
            if history.contains(&marker) || history.len() >= 5000 {
                return Err(ApiError::bad("Pixiv 分页重复或超过 5000 页，旧目录已保留").into());
            }
            history.push(marker);
        }
        if !works.is_empty() || cursor.is_null() {
            break;
        }
    }
    let mut tx = s.db.begin().await?;
    ctx.guard_tx(&mut tx).await?;
    let current = tx
        .query("SELECT * FROM author_feeds WHERE id=?", &[json!(fid)])
        .await?
        .pop()
        .filter(|v| v["revision"] == p["revision"])
        .ok_or_else(|| ApiError::bad("作者目录已删除或更新，结果未保存"))?;
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
    let sub=tx.query("SELECT * FROM subscriptions WHERE platform='pixiv' AND target_type='author' AND target=? ORDER BY created_at,id LIMIT 1",&[json!(ident)]).await?.pop();
    let mut download = false;
    if let Some(sub) = sub.filter(|v| {
        truth(&v["enabled"])
            && (v["content_scope"] == "all"
                || (v["content_scope"] == "novels") == (category == "novel"))
    }) {
        let default =
            truth(&executor::setting_tx(s, &mut tx, subscriptions::FOLLOW_KEY).await?["enabled"]);
        let (w, b) = subscriptions::flags(&sub, default);
        download = if section == "works" {
            w
        } else {
            b && restrict == "public"
        };
    }
    let mut queued = HashSet::new();
    let mut seen = HashSet::new();
    for (index, w) in works.iter().enumerate() {
        if !seen.insert(w.remote_id.clone()) {
            return Err(ApiError::bad("作者分页包含重复作品，旧目录已保留").into());
        }
        if deleted.contains(w.remote_id.as_str()) {
            unavailable += 1;
            continue;
        }
        let item = content_store::merge(s, &mut tx, "pixiv", w).await?;
        let id = item["id"].as_str().unwrap();
        executor::observe(s, &mut tx, id, w, owner.as_deref(), &Value::Null).await?;
        let sql = format!(
            "INSERT INTO author_feed_entries(feed_id,item_id,generation,position) VALUES(?,?,?,?) {}",
            s.db.upsert("generation,position", "feed_id,item_id")
        );
        let prior = tx
            .query(
                "SELECT generation FROM author_feed_entries WHERE feed_id=? AND item_id=?",
                &[json!(fid), json!(id)],
            )
            .await?
            .pop();
        if prior.is_none_or(|v| v["generation"] != generation) {
            tx.execute(
                &sql,
                &[
                    json!(fid),
                    json!(id),
                    json!(generation),
                    json!(raw_count - works.len() as i64 + index as i64),
                ],
            )
            .await?;
        }
        if tx
            .query(
                "SELECT id FROM assets WHERE item_id=? LIMIT 1",
                &[json!(id)],
            )
            .await?
            .is_empty()
            && (json_column(&item["preview_cover"]).is_null() || download)
        {
            let j=tasks::enqueue_import_tx(s,&mut tx,json!({"platform":"pixiv","remote_id":w.remote_id,"item_id":id,"account_id":p["account_id"],"download":download})).await?;
            queued.insert(j["id"].as_str().unwrap().to_owned());
        }
    }
    let complete = cursor.is_null() && unavailable == 0;
    if complete {
        tx.execute(
            "DELETE FROM author_feed_entries WHERE feed_id=? AND generation!=?",
            &[json!(fid), json!(generation)],
        )
        .await?;
    }
    tx.execute("UPDATE author_feeds SET generation=?,revision=revision+1,next_cursor=?,seen_cursors=?,raw_count=?,unavailable=?,complete=?,updated_at=? WHERE id=?",&[json!(generation),if cursor.is_null(){Value::Null}else{json!(cursor.to_string())},json!(json!(history).to_string()),json!(raw_count),json!(unavailable),json!(complete),json!(now()),current["id"].clone()]).await?;
    tx.commit().await?;
    Ok(
        json!({"author_id":ident,"feed_id":fid,"queued_covers":queued.len(),"has_more":!cursor.is_null(),"complete":complete,"message":if download{"目录已保存，缺失正文已进入自动下载队列"}else{"目录已保存，缺失封面已排队；正文需另行下载"}}),
    )
}
