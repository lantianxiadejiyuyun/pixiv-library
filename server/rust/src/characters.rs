//! Explicit public research is separate from local recognition. Provider HTML
//! is inert text; no external image is sent without the image-search consent.
use crate::{
    ApiJson, AppState, content_store,
    db::{iso, json_column, now},
    error::{ApiError, Result},
    library::{self, Params},
    media, network, queue, secrets,
};
use axum::{
    Json,
    extract::{Multipart, Path, Query, State},
    http::StatusCode,
};
use futures_util::StreamExt;
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    sync::{Mutex, OnceLock},
    time::{Duration, Instant},
};
use unicode_normalization::UnicodeNormalization;
const NOTICE: &str =
    "这些是公开角色资料候选，不是对书页人物的识别结论；请核对作品和来源后再人工确认。";
const FIELDS: &str = "id name { full native alternative } description(asHtml: false) siteUrl";
const WORKS: &str = "media(perPage: 3, sort: POPULARITY_DESC) { nodes { id type title { romaji english native } siteUrl } }";
static TEXT_RATE: OnceLock<Mutex<VecDeque<Instant>>> = OnceLock::new();
static IMAGE_RATE: OnceLock<Mutex<VecDeque<Instant>>> = OnceLock::new();
fn throttle(rate: &OnceLock<Mutex<VecDeque<Instant>>>, limit: usize) -> Result<()> {
    let mut attempts = rate
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| ApiError::bad("搜索服务忙"))?;
    let now = Instant::now();
    while attempts
        .front()
        .is_some_and(|v| now.duration_since(*v) >= Duration::from_secs(60))
    {
        attempts.pop_front();
    }
    if attempts.len() >= limit {
        return Err(ApiError(
            StatusCode::TOO_MANY_REQUESTS,
            "角色搜索较频繁，请稍后重试",
        ));
    }
    attempts.push_back(now);
    Ok(())
}
fn role(name: &str) -> &str {
    for prefix in ["角色:", "角色：", "人物:", "人物：", "character:"] {
        if name.to_lowercase().starts_with(prefix) {
            return name[prefix.len()..].trim();
        }
    }
    ""
}
async fn tag(s: &AppState, id: &str) -> Result<Value> {
    let row =
        s.db.one("SELECT * FROM tags WHERE id=?", &[json!(id)])
            .await?
            .ok_or(ApiError::missing("人物或作品不存在"))?;
    if role(row["name"].as_str().unwrap_or("")).is_empty() {
        return Err(ApiError::missing("人物或作品不存在"));
    }
    Ok(row)
}
fn profile(row: &Value) -> Value {
    if row["provider"].is_null() {
        return Value::Null;
    }
    json!({"provider":row["provider"],"id":row["provider_id"],"name":row["profile_name"],"native_name":row["native_name"],"aliases":json_column(&row["aliases"]),"description":row["description"],"works":json_column(&row["works"]),"source_url":row["source_url"],"updated_at":iso(&row["updated_at"])})
}
fn view(row: &Value, covers: Value) -> Value {
    let name = role(row["name"].as_str().unwrap_or(""));
    let zh = row["name_zh"].as_str().unwrap_or("");
    let display = if !zh.is_empty() {
        if role(zh).is_empty() { zh } else { role(zh) }
    } else {
        row["native_name"]
            .as_str()
            .filter(|x| !x.is_empty())
            .unwrap_or(name)
    };
    json!({"id":row["id"],"tag_name":row["name"],"name":name,"display_name":display,"count":row["count"],"profile":profile(row),"covers":covers})
}
fn links(q: &Params) -> Result<(String, Vec<Value>)> {
    let mut filter = vec!["1=1".to_owned()];
    let mut args = vec![];
    if !library::text(q, "source").is_empty() {
        filter.push("i.source=?".into());
        args.push(json!(library::text(q, "source")));
    }
    if library::flag(q, "downloaded")? {
        filter.push(format!("({})", library::DOWNLOADED));
    }
    Ok((
        format!(
            "SELECT DISTINCT it.tag_id,i.id AS item_id,i.created_at FROM item_tags it JOIN items i ON i.id=it.item_id WHERE {}",
            filter.join(" AND ")
        ),
        args,
    ))
}
async fn samples(
    s: &AppState,
    ids: &[String],
    links: &str,
    args: &[Value],
) -> Result<std::collections::HashMap<String, Value>> {
    let mut result = std::collections::HashMap::new();
    if ids.is_empty() {
        return Ok(result);
    }
    let mut a = args.to_vec();
    a.extend(ids.iter().map(|id| json!(id)));
    let sql = format!(
        "SELECT r.tag_id,i.id,i.title,i.title_zh,i.title_zh_source,i.rating,i.preview_cover,(SELECT a.id FROM assets a WHERE a.item_id=i.id AND a.status='available' AND a.mime_type LIKE 'image/%' ORDER BY a.position,a.id LIMIT 1) AS cover_asset FROM (SELECT l.*,ROW_NUMBER() OVER(PARTITION BY l.tag_id ORDER BY l.created_at DESC,l.item_id) AS rn FROM ({links}) l WHERE l.tag_id IN ({})) r JOIN items i ON i.id=r.item_id WHERE r.rn<=3 ORDER BY r.tag_id,r.rn",
        library::placeholders(ids.len())
    );
    for row in s.db.query(&sql, &a).await? {
        let id = row["id"].as_str().unwrap_or("");
        let url = if let Some(asset) = row["cover_asset"].as_str() {
            json!(format!("/api/assets/{asset}/thumbnail"))
        } else if json_column(&row["preview_cover"]).is_object() {
            json!(format!("/api/items/{id}/cover"))
        } else {
            Value::Null
        };
        let title = if row["title_zh"].as_str().is_some_and(|v| !v.is_empty())
            && row["title_zh_source"] == row["title"]
        {
            &row["title_zh"]
        } else {
            &row["title"]
        };
        let v = result
            .entry(row["tag_id"].as_str().unwrap_or("").to_owned())
            .or_insert_with(|| json!([]));
        v.as_array_mut()
            .unwrap()
            .push(json!({"id":id,"title":title,"cover_url":url,"rating":row["rating"]}));
    }
    Ok(result)
}
const COLUMNS: &str = "t.*,p.provider,p.provider_id,p.name AS profile_name,p.native_name,p.aliases,p.description,p.works,p.source_url,p.updated_at,COALESCE(totals.count,0) AS count";
const RECOGNIZED: &str = "(((t.name LIKE '角色:%' OR t.name LIKE '角色：%' OR t.name LIKE '人物:%' OR t.name LIKE '人物：%') AND TRIM(SUBSTR(t.name,4))<>'') OR (LOWER(t.name) LIKE 'character:%' AND TRIM(SUBSTR(t.name,11))<>''))";
pub async fn list(State(s): State<AppState>, Query(q): Query<Params>) -> Result<Json<Value>> {
    let page = library::number(&q, "page", 1, 1, 1_000_000)?;
    let size = library::number(&q, "page_size", 24, 1, 60)?;
    let sort = library::text(&q, "sort");
    if !matches!(sort, "" | "count" | "name") {
        return Err(ApiError::invalid("人物排序无效"));
    }
    let (links, mut args) = links(&q)?;
    let include = library::text(&q, "source").is_empty() && !library::flag(&q, "downloaded")?;
    let mut base = format!(
        "FROM tags t LEFT JOIN character_profiles p ON p.tag_id=t.id LEFT JOIN (SELECT tag_id,COUNT(*) AS count FROM ({links}) l GROUP BY tag_id) totals ON totals.tag_id=t.id WHERE {RECOGNIZED} AND (COALESCE(totals.count,0)>0 {})",
        if include {
            "OR p.tag_id IS NOT NULL"
        } else {
            ""
        }
    );
    let term = library::text(&q, "q").trim().nfc().collect::<String>();
    if term.chars().count() > 200 {
        return Err(ApiError::invalid("搜索过长"));
    }
    if !term.is_empty() {
        base.push_str(" AND (LOWER(t.name) LIKE LOWER(?) ESCAPE '!' OR LOWER(t.name_zh) LIKE LOWER(?) ESCAPE '!' OR LOWER(p.search_text) LIKE LOWER(?) ESCAPE '!')");
        args.extend(std::iter::repeat_n(
            json!(format!("%{}%", library::like(&term))),
            3,
        ));
    }
    let total =
        s.db.count(&format!("SELECT COUNT(*) {base}"), &args)
            .await?;
    let order = if sort == "name" {
        "COALESCE(t.name_zh,t.name)"
    } else {
        "count DESC"
    };
    args.extend([json!(size), json!((page - 1) * size)]);
    let rows =
        s.db.query(
            &format!("SELECT {COLUMNS} {base} ORDER BY {order},t.id LIMIT ? OFFSET ?"),
            &args,
        )
        .await?;
    let ids = rows
        .iter()
        .filter_map(|r| r["id"].as_str().map(str::to_owned))
        .collect::<Vec<_>>();
    let (_, linkargs) = self::links(&q)?;
    let samples = samples(&s, &ids, &links, &linkargs).await?;
    Ok(Json(
        json!({"items":rows.iter().filter(|r|!role(r["name"].as_str().unwrap_or("")).is_empty()).map(|r|view(r,samples.get(r["id"].as_str().unwrap_or("")).cloned().unwrap_or(json!([])))).collect::<Vec<_>>(),"total":total,"page":page,"page_size":size}),
    ))
}
async fn detail_value(s: &AppState, id: &str, q: &Params) -> Result<Value> {
    tag(s, id).await?;
    let (links, mut args) = links(q)?;
    args.push(json!(id));
    let row=s.db.one(&format!("SELECT {COLUMNS} FROM tags t LEFT JOIN character_profiles p ON p.tag_id=t.id LEFT JOIN (SELECT tag_id,COUNT(*) AS count FROM ({links}) l GROUP BY tag_id) totals ON totals.tag_id=t.id WHERE t.id=?"),&args).await?.ok_or(ApiError::missing("人物或作品不存在"))?;
    let (_, a) = self::links(q)?;
    let covers = samples(s, &[id.into()], &links, &a)
        .await?
        .remove(id)
        .unwrap_or(json!([]));
    Ok(view(&row, covers))
}
pub async fn detail(
    State(s): State<AppState>,
    Path(id): Path<String>,
    Query(q): Query<Params>,
) -> Result<Json<Value>> {
    Ok(Json(detail_value(&s, &id, &q).await?))
}
pub fn clean(raw: &Value, limit: usize) -> String {
    let raw = raw.as_str().unwrap_or("");
    let without_images = regex::Regex::new(r"!\[[^\]]*\]\([^)]*\)")
        .unwrap()
        .replace_all(raw, "");
    let plain = crate::platforms::kemono::document(&without_images).text;
    plain
        .chars()
        .filter(|c| !c.is_control() || *c == '\n' || *c == '\t')
        .take(limit)
        .collect::<String>()
        .trim()
        .to_owned()
}
fn work(v: &Value) -> Option<Value> {
    let id = v["id"]
        .as_u64()
        .filter(|n| *n > 0 && *n <= i32::MAX as u64)?;
    let kind = match v["type"].as_str()? {
        "ANIME" => "anime",
        "MANGA" => "manga",
        _ => return None,
    };
    let title = ["native", "english", "romaji"]
        .iter()
        .map(|key| clean(&v["title"][key], 300))
        .find(|s| !s.is_empty())?;
    Some(json!({"title":title,"type":kind,"source_url":format!("https://anilist.co/{kind}/{id}")}))
}
pub fn candidate(v: &Value, works: Option<Vec<Value>>) -> Option<Value> {
    let id = v["id"]
        .as_u64()
        .filter(|n| *n > 0 && *n <= i32::MAX as u64)?;
    let name = clean(
        if v["name"]["full"].as_str().is_some_and(|s| !s.is_empty()) {
            &v["name"]["full"]
        } else {
            &v["name"]["native"]
        },
        188,
    );
    if name.is_empty() {
        return None;
    }
    let native = clean(&v["name"]["native"], 188);
    let mut aliases = vec![];
    for alias in v["name"]["alternative"]
        .as_array()
        .into_iter()
        .flatten()
        .take(30)
    {
        let a = clean(alias, 188);
        if !a.is_empty() && !aliases.contains(&a) {
            aliases.push(a);
        }
    }
    let works = works.unwrap_or_else(|| {
        v["media"]["nodes"]
            .as_array()
            .into_iter()
            .flatten()
            .take(3)
            .filter_map(work)
            .collect()
    });
    Some(
        json!({"provider":"AniList","id":id.to_string(),"name":name,"native_name":if native.is_empty(){Value::Null}else{json!(native)},"aliases":aliases,"description":clean(&v["description"],1600),"works":works,"source_url":format!("https://anilist.co/character/{id}")}),
    )
}
async fn response_json(response: reqwest::Response, limit: usize) -> Result<Value> {
    if !response.status().is_success() {
        return Err(ApiError(
            if response.status().as_u16() == 429 {
                StatusCode::TOO_MANY_REQUESTS
            } else {
                StatusCode::BAD_GATEWAY
            },
            "角色资料服务暂不可用，请检查额度、网络或代理",
        ));
    }
    let mut data = vec![];
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|_| ApiError::bad("资料响应读取失败"))?;
        if data.len() + chunk.len() > limit {
            return Err(ApiError::bad("资料响应过大"));
        }
        data.extend_from_slice(&chunk);
    }
    serde_json::from_slice(&data).map_err(|_| ApiError::bad("资料服务响应格式无效"))
}
async fn graphql(s: &AppState, query: String, vars: Value) -> Result<Value> {
    throttle(&TEXT_RATE, 20)?;
    let response = network::content_client(s)
        .await?
        .post("https://graphql.anilist.co")
        .timeout(Duration::from_secs(20))
        .json(&json!({"query":query,"variables":vars}))
        .send()
        .await
        .map_err(|_| {
            ApiError(
                StatusCode::BAD_GATEWAY,
                "无法连接角色资料服务，请检查网络与代理",
            )
        })?;
    let v = response_json(response, 1024 * 1024).await?;
    if !v["errors"].is_null() || !v["data"].is_object() {
        return Err(ApiError::bad("人物资料服务返回无效结果"));
    }
    Ok(v["data"].clone())
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Search {
    query: String,
    #[serde(default = "character_mode")]
    mode: String,
    #[serde(default = "six")]
    limit: usize,
}
fn character_mode() -> String {
    "character".into()
}
fn six() -> usize {
    6
}
pub async fn search(State(s): State<AppState>, ApiJson(v): ApiJson<Search>) -> Result<Json<Value>> {
    let term = v.query.trim().nfc().collect::<String>();
    if term.is_empty()
        || term.chars().count() > 120
        || term.chars().any(char::is_control)
        || !(1..=10).contains(&v.limit)
        || !matches!(v.mode.as_str(), "character" | "work")
    {
        return Err(ApiError::invalid("请输入有效角色名或作品名"));
    }
    let query = if v.mode == "character" {
        format!(
            "query($query:String!,$limit:Int!){{Page(page:1,perPage:$limit){{characters(search:$query,sort:SEARCH_MATCH){{{FIELDS} {WORKS}}}}}}}"
        )
    } else {
        format!(
            "query($query:String!,$limit:Int!){{Page(page:1,perPage:3){{media(search:$query,type:MANGA,sort:SEARCH_MATCH){{id type title{{romaji english native}} characters(perPage:$limit,sort:[ROLE,RELEVANCE]){{nodes{{{FIELDS}}}}}}}}}}}"
        )
    };
    let data = graphql(&s, query, json!({"query":term,"limit":v.limit})).await?;
    let rows = data["Page"][if v.mode == "character" {
        "characters"
    } else {
        "media"
    }]
    .as_array()
    .ok_or(ApiError::bad("角色资料响应格式无效"))?;
    let mut candidates: Vec<Value> = vec![];
    if v.mode == "character" {
        candidates.extend(rows.iter().take(v.limit).filter_map(|v| candidate(v, None)));
    } else {
        for index in 0..v.limit {
            for book in rows.iter().take(3) {
                if let Some(w) = work(book)
                    && let Some(c) =
                        candidate(&book["characters"]["nodes"][index], Some(vec![w.clone()]))
                {
                    if let Some(old) = candidates.iter_mut().find(|old| old["id"] == c["id"]) {
                        let works = old["works"].as_array_mut().unwrap();
                        if !works.contains(&w) {
                            works.push(w);
                        }
                    } else if candidates.len() < v.limit {
                        candidates.push(c);
                    }
                }
            }
        }
    }
    if !rows.is_empty() && candidates.is_empty() {
        return Err(ApiError::bad("资料候选无法解析"));
    }
    Ok(Json(
        json!({"provider":"AniList","query":term,"mode":v.mode,"candidates":candidates,"notice":NOTICE}),
    ))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProfileInput {
    candidate_id: u32,
}
async fn save_profile(s: &AppState, id: Option<String>, v: ProfileInput) -> Result<Json<Value>> {
    s.config.writable()?;
    if v.candidate_id == 0 || v.candidate_id > i32::MAX as u32 {
        return Err(ApiError::invalid("人物编号无效"));
    }
    if let Some(id) = &id {
        tag(s, id).await?;
    }
    let data = graphql(
        s,
        format!("query($id:Int!){{Character(id:$id){{{FIELDS} {WORKS}}}}}"),
        json!({"id":v.candidate_id}),
    )
    .await?;
    let expected_id = v.candidate_id.to_string();
    let c = candidate(&data["Character"], None)
        .filter(|c| c["id"].as_str() == Some(expected_id.as_str()))
        .ok_or(ApiError::missing("人物或作品不存在"))?;
    let mut tx = s.db.begin().await?;
    queue::locked(s, &mut tx).await?;
    let ident = if let Some(id) = id {
        let exists = tx
            .query("SELECT name FROM tags WHERE id=?", &[json!(id)])
            .await?;
        if exists
            .first()
            .is_none_or(|v| role(v["name"].as_str().unwrap_or("")).is_empty())
        {
            return Err(ApiError::missing("人物或作品不存在"));
        }
        id
    } else {
        let name = c["native_name"]
            .as_str()
            .unwrap_or(c["name"].as_str().unwrap_or(""));
        content_store::ensure_tag(
            s,
            &mut tx,
            &format!("角色:{}", name.chars().take(188).collect::<String>()),
        )
        .await?
    };
    let search = [
        c["name"].as_str().unwrap_or(""),
        c["native_name"].as_str().unwrap_or(""),
    ]
    .into_iter()
    .chain(
        c["aliases"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str),
    )
    .collect::<Vec<_>>()
    .join("\n");
    let sql=format!("INSERT INTO character_profiles(tag_id,provider,provider_id,name,native_name,aliases,description,works,source_url,search_text,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) {}",s.db.upsert("provider,provider_id,name,native_name,aliases,description,works,source_url,search_text,updated_at","tag_id"));
    tx.execute(
        &sql,
        &[
            json!(ident),
            c["provider"].clone(),
            c["id"].clone(),
            c["name"].clone(),
            c["native_name"].clone(),
            json!(c["aliases"].to_string()),
            c["description"].clone(),
            json!(c["works"].to_string()),
            c["source_url"].clone(),
            json!(search),
            json!(now()),
            json!(now()),
        ],
    )
    .await?;
    tx.commit().await?;
    Ok(Json(detail_value(s, &ident, &Params::new()).await?))
}
pub async fn add(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<ProfileInput>,
) -> Result<Json<Value>> {
    save_profile(&s, None, v).await
}
pub async fn update(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(v): ApiJson<ProfileInput>,
) -> Result<Json<Value>> {
    save_profile(&s, Some(id), v).await
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Confirm {
    item_id: String,
    name: String,
}
pub async fn confirm(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<Confirm>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let name = v
        .name
        .trim()
        .strip_prefix("角色:")
        .unwrap_or(v.name.trim())
        .trim()
        .nfc()
        .collect::<String>();
    if name.is_empty() || name.chars().count() > 188 || name.chars().any(char::is_control) {
        return Err(ApiError::invalid("角色名称无效"));
    }
    let name = format!("角色:{name}");
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    if tx
        .query("SELECT id FROM items WHERE id=?", &[json!(v.item_id)])
        .await?
        .is_empty()
    {
        return Err(ApiError::missing("人物或作品不存在"));
    }
    let tag = content_store::ensure_tag(&s, &mut tx, &name).await?;
    let sql = if s.db.mysql() {
        "INSERT INTO item_tags(item_id,tag_id,origin,score) VALUES(?,?,'manual',NULL) ON DUPLICATE KEY UPDATE origin='manual'"
    } else {
        "INSERT INTO item_tags(item_id,tag_id,origin,score) VALUES(?,?,'manual',NULL) ON CONFLICT(item_id,tag_id,origin) DO NOTHING"
    };
    tx.execute(sql, &[json!(v.item_id), json!(tag)]).await?;
    tx.commit().await?;
    Ok(Json(
        json!({"item_id":v.item_id,"tag":{"id":tag,"name":name,"origin":"manual","score":null}}),
    ))
}
pub async fn image_settings(State(s): State<AppState>) -> Result<Json<Value>> {
    let key = secrets::get(&s, "character_image_search")
        .await?
        .unwrap_or(json!({}));
    Ok(Json(
        json!({"provider":"SauceNAO","has_api_key":key["api_key"].as_str().is_some_and(|k|!k.is_empty()),"max_file_bytes":8*1024*1024,"notice":"仅在点击上传搜索时向 SauceNAO 发送选中的一张图片；图片会缩小并移除元数据。"}),
    ))
}
#[derive(Deserialize)]
pub struct ImageSettings {
    api_key: Option<String>,
}
pub async fn image_save(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<ImageSettings>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    if let Some(key) = v.api_key {
        let key = key.trim();
        if key.len() > 256
            || !key
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
        {
            return Err(ApiError::invalid("图搜密钥格式无效"));
        }
        secrets::put(&s, "character_image_search", &json!({"api_key":key})).await?;
    }
    image_settings(State(s)).await
}
pub fn image_matches(v: &Value) -> Result<Vec<Value>> {
    if v["header"]["status"]
        .as_i64()
        .or_else(|| v["header"]["status"].as_str().and_then(|s| s.parse().ok()))
        != Some(0)
    {
        return Err(ApiError(
            StatusCode::BAD_GATEWAY,
            "图搜未完成，请检查密钥或额度",
        ));
    }
    let rows = v["results"]
        .as_array()
        .ok_or(ApiError::bad("图搜结果格式无效"))?;
    let mut out = vec![];
    for row in rows.iter().take(20) {
        let Some(sim) = row["header"]["similarity"].as_f64().or_else(|| {
            row["header"]["similarity"]
                .as_str()
                .and_then(|s| s.parse().ok())
        }) else {
            continue;
        };
        if !sim.is_finite() || !(0.0..=100.0).contains(&sim) {
            continue;
        }
        let d = &row["data"];
        if !d.is_object() {
            continue;
        }
        let choose = |keys: &[&str], len| {
            keys.iter()
                .map(|k| sauce_text(&d[k], len))
                .find(|s| !s.is_empty())
                .unwrap_or_default()
        };
        let chars = if let Some(s) = d["characters"].as_str() {
            s.split(',').map(|s| json!(s)).collect::<Vec<_>>()
        } else {
            d["characters"].as_array().cloned().unwrap_or_default()
        };
        let characters = chars
            .iter()
            .take(20)
            .map(|s| sauce_text(s, 120))
            .filter(|s| !s.is_empty())
            .collect::<std::collections::BTreeSet<_>>();
        let urls = d["ext_urls"]
            .as_array()
            .into_iter()
            .flatten()
            .take(10)
            .filter_map(Value::as_str)
            .filter_map(safe_url)
            .collect::<std::collections::BTreeSet<_>>();
        let title = choose(&["title", "eng_name", "jp_name", "source"], 300);
        out.push(json!({"similarity":(sim*100.0).round()/100.0,"index_name":sauce_text(&row["header"]["index_name"],120),"title":if title.is_empty(){"未提供标题"}else{&title},"creator":choose(&["creator","member_name","author_name"],160),"characters":characters,"material":sauce_text(&d["material"],200),"source_urls":urls}));
    }
    if !rows.is_empty() && out.is_empty() {
        return Err(ApiError::bad("图搜候选无法解析"));
    }
    out.truncate(10);
    Ok(out)
}
fn sauce_text(v: &Value, len: usize) -> String {
    let raw = if let Some(a) = v.as_array() {
        a.iter()
            .take(12)
            .filter_map(|v| {
                v.as_str()
                    .map(str::to_owned)
                    .or_else(|| v.as_i64().map(|n| n.to_string()))
            })
            .collect::<Vec<_>>()
            .join(" / ")
    } else {
        v.as_str()
            .map(str::to_owned)
            .or_else(|| v.as_i64().map(|n| n.to_string()))
            .unwrap_or_default()
    };
    raw.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(len)
        .collect()
}
fn safe_url(raw: &str) -> Option<String> {
    if raw.len() > 2048
        || raw.contains('\\')
        || raw.chars().any(|c| c.is_control() || c.is_whitespace())
    {
        return None;
    }
    let u = url::Url::parse(raw).ok()?;
    let h = u.host_str()?;
    if !matches!(u.scheme(), "http" | "https")
        || !u.username().is_empty()
        || u.password().is_some()
        || u.port().is_some_and(|p| !matches!(p, 80 | 443))
        || !h.contains('.')
        || [
            ".local",
            ".localhost",
            ".lan",
            ".internal",
            ".home",
            ".localdomain",
        ]
        .iter()
        .any(|s| h.ends_with(s))
        || h.parse().is_ok_and(|ip| !network::public_ip(ip))
    {
        return None;
    }
    Some(raw.into())
}
pub async fn image_search(
    State(s): State<AppState>,
    mut multipart: Multipart,
) -> Result<Json<Value>> {
    let mut file = None;
    let mut asset = None;
    let mut consent = false;
    let mut count = 0;
    while let Some(mut field) = multipart
        .next_field()
        .await
        .map_err(|_| ApiError::bad("上传格式无效"))?
    {
        count += 1;
        if count > 3 {
            return Err(ApiError::bad("上传字段过多"));
        }
        let name = field.name().unwrap_or("").to_owned();
        let mut data = vec![];
        while let Some(chunk) = field
            .chunk()
            .await
            .map_err(|_| ApiError::bad("上传读取失败"))?
        {
            let max = if name == "file" { 8 * 1024 * 1024 } else { 100 };
            if data.len() + chunk.len() > max {
                return Err(ApiError(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "请使用不超过 8 MiB 的图片",
                ));
            }
            data.extend_from_slice(&chunk);
        }
        match name.as_str() {
            "file" if file.is_none() => file = Some(data),
            "asset_id" if asset.is_none() => {
                asset = Some(String::from_utf8(data).map_err(|_| ApiError::bad("书页编号无效"))?)
            }
            "consent" => consent = matches!(data.as_slice(), b"true" | b"1" | b"on"),
            _ => return Err(ApiError::bad("上传字段无效或重复")),
        }
    }
    if !consent {
        return Err(ApiError::bad("请确认将选中的这一张图片上传至 SauceNAO"));
    }
    if file.is_some() == asset.as_ref().is_some_and(|s| !s.is_empty()) {
        return Err(ApiError::bad("请选择一张图片或一个书页，不能同时提交"));
    }
    let data = if let Some(file) = file {
        file
    } else {
        let (row, path) = media::asset(&s, asset.as_deref().unwrap_or("")).await?;
        if !row["mime_type"]
            .as_str()
            .is_some_and(|m| m.starts_with("image/"))
            || row["size_bytes"].as_u64().unwrap_or(u64::MAX) > 8 * 1024 * 1024
        {
            return Err(ApiError::bad("请选择不超过 8 MiB 的图片书页"));
        }
        media::io(&s, move || {
            use std::io::Read;
            let mut b = vec![];
            std::fs::File::open(path)?
                .take(8 * 1024 * 1024 + 1)
                .read_to_end(&mut b)?;
            Ok(b)
        })
        .await?
    };
    let permit = s
        .cpu_limit
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| ApiError::bad("图像服务不可用"))?;
    let image = tokio::task::spawn_blocking(move || -> Result<Vec<u8>> {
        let _permit = permit;
        let mut reader =
            image::ImageReader::new(std::io::Cursor::new(data)).with_guessed_format()?;
        if !matches!(
            reader.format(),
            Some(
                image::ImageFormat::Jpeg
                    | image::ImageFormat::Png
                    | image::ImageFormat::WebP
                    | image::ImageFormat::Gif
            )
        ) {
            return Err(ApiError::bad("仅支持 JPEG、PNG、WebP 或 GIF 图片"));
        }
        let mut limits = image::Limits::default();
        limits.max_alloc = Some(192 * 1024 * 1024);
        limits.max_image_width = Some(16384);
        limits.max_image_height = Some(16384);
        reader.limits(limits);
        let mut decoder = reader
            .into_decoder()
            .map_err(|_| ApiError::bad("图片格式无效"))?;
        use image::ImageDecoder;
        let (width, height) = decoder.dimensions();
        if u64::from(width) * u64::from(height) > 36_000_000 {
            return Err(ApiError::bad("图片像素过多"));
        }
        let orientation = decoder
            .orientation()
            .unwrap_or(image::metadata::Orientation::NoTransforms);
        let mut image =
            image::DynamicImage::from_decoder(decoder).map_err(|_| ApiError::bad("图片损坏"))?;
        image.apply_orientation(orientation);
        let small = image.thumbnail(1024, 1024).into_rgba8();
        let mut white = image::RgbImage::new(small.width(), small.height());
        for (x, y, p) in small.enumerate_pixels() {
            let a = u16::from(p[3]);
            white.put_pixel(
                x,
                y,
                image::Rgb(
                    [0, 1, 2].map(|c| ((u16::from(p[c]) * a + 255 * (255 - a)) / 255) as u8),
                ),
            );
        }
        let mut output = vec![];
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut output, 90)
            .encode_image(&white)
            .map_err(|_| ApiError::bad("图片转码失败"))?;
        Ok(output)
    })
    .await
    .map_err(|_| ApiError::bad("图像处理失败"))??;
    throttle(&IMAGE_RATE, 6)?;
    let key = secrets::get(&s, "character_image_search")
        .await?
        .unwrap_or(json!({}));
    let size = image.len();
    let mut form = reqwest::multipart::Form::new()
        .text("output_type", "2")
        .text("numres", "10")
        .text("db", "999")
        .part(
            "file",
            reqwest::multipart::Part::bytes(image)
                .file_name("image.jpg")
                .mime_str("image/jpeg")
                .map_err(|_| ApiError::bad("图像格式无效"))?,
        );
    if let Some(key) = key["api_key"].as_str().filter(|k| !k.is_empty()) {
        form = form.text("api_key", key.to_owned());
    }
    let response = network::content_client(&s)
        .await?
        .post("https://saucenao.com/search.php")
        .timeout(Duration::from_secs(30))
        .multipart(form)
        .send()
        .await
        .map_err(|_| ApiError(StatusCode::BAD_GATEWAY, "图搜连接失败，请检查网络与代理"))?;
    let value = response_json(response, 2 * 1024 * 1024).await?;
    Ok(Json(
        json!({"provider":"SauceNAO","matches":image_matches(&value)?,"uploaded_size":size,"notice":"相似度表示图片匹配程度，不是人物身份置信度。请结合出处核对。"}),
    ))
}
