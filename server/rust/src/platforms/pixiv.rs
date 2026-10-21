//! Direct implementation of the App API protocol previously used by PixivPy.
//! OAuth always stays at the official origin; bearer tokens only reach the
//! explicitly configured App API. Media requests never receive account tokens.
use super::{id, plain, positive, preview, sensitive, str_field, suffix};
use crate::{
    AppState,
    content_store::Work,
    error::{ApiError, Result},
    network,
    tasks::{Context, TaskResult},
    transport::{RequestBody, SafeHttp},
};
use reqwest::{
    Method,
    header::{AUTHORIZATION, HeaderMap, HeaderValue, REFERER, USER_AGENT},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, Mutex, OnceLock},
    time::{Duration, Instant},
};
pub const CLIENT_ID: &str = "MOBrBDS8blbauoSck0ZfDbtuzpyT";
pub const CLIENT_SECRET: &str = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj";
pub const HASH_SECRET: &str = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c";
pub const UA: &str = "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)";
#[derive(Clone)]
struct Token {
    access: String,
    user: String,
    expires: Instant,
}
type TokenCell = Arc<tokio::sync::Mutex<Option<Token>>>;
static TOKENS: OnceLock<Mutex<HashMap<String, TokenCell>>> = OnceLock::new();
fn token_cell(refresh: &str) -> Result<TokenCell> {
    let key = format!("{:x}", Sha256::digest(refresh));
    let mut cache = TOKENS
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| ApiError::bad("登录缓存不可用"))?;
    if cache.len() >= 256 && !cache.contains_key(&key) {
        cache.retain(|_, v| Arc::strong_count(v) > 1);
        if cache.len() >= 256 {
            return Err(ApiError::bad("同时登录的账号过多，请稍后重试"));
        }
    }
    Ok(cache
        .entry(key)
        .or_insert_with(|| Arc::new(tokio::sync::Mutex::new(None)))
        .clone())
}
pub fn headers() -> HeaderMap {
    let mut h = HeaderMap::new();
    h.insert(USER_AGENT, HeaderValue::from_static(UA));
    h.insert("app-os", HeaderValue::from_static("ios"));
    h.insert("app-os-version", HeaderValue::from_static("14.6"));
    h
}
pub async fn oauth(
    s: &AppState,
    form: Vec<(String, String)>,
    ctx: Option<Context>,
) -> TaskResult<Value> {
    let http = SafeHttp::new(s, vec!["oauth.secure.pixiv.net".into()], ctx).await?;
    let time = chrono::Utc::now()
        .format("%Y-%m-%dT%H:%M:%S+00:00")
        .to_string();
    let hash = format!("{:x}", md5::Md5::digest(format!("{time}{HASH_SECRET}")));
    let mut h = headers();
    h.insert("x-client-time", HeaderValue::from_str(&time).unwrap());
    h.insert("x-client-hash", HeaderValue::from_str(&hash).unwrap());
    let mut form = form;
    form.extend([
        ("client_id".into(), CLIENT_ID.into()),
        ("client_secret".into(), CLIENT_SECRET.into()),
        ("include_policy".into(), "true".into()),
        ("get_secure_url".into(), "1".into()),
    ]);
    let result = http
        .json(
            Method::POST,
            "https://oauth.secure.pixiv.net/auth/token",
            h,
            RequestBody::Form(form),
        )
        .await?;
    if !result.is_object() || !result["error"].is_null() && result["error"] != false {
        return Err(ApiError::bad("Pixiv 登录失败，请检查凭据与网络").into());
    }
    Ok(result.get("response").cloned().unwrap_or(result))
}
pub fn normalize(input: &str) -> Result<String> {
    let input = input.trim();
    if positive(input.strip_prefix("novel:").unwrap_or(input)).is_ok() {
        return Ok(input.into());
    }
    let u = network::validate_url(input, &["pixiv.net".into()])?;
    let re = regex::Regex::new(r"^/(?:[a-z]{2}/)?artworks/([1-9]\d{0,19})/?$").unwrap();
    if let Some(c) = re.captures(u.path()) {
        return Ok(c[1].into());
    }
    if u.path() == "/novel/show.php"
        && let Some((_, value)) = u.query_pairs().find(|(k, _)| k == "id")
    {
        positive(&value)?;
        return Ok(format!("novel:{value}"));
    }
    Err(ApiError::bad("请输入 Pixiv 作品链接、ID 或 novel:小说ID"))
}
pub fn metadata(value: &Value, novel: bool) -> Result<Work> {
    if value["visible"] == false {
        return Err(ApiError::bad(
            "Pixiv 作品对当前账号不可见，请检查 R18 浏览设置和访问权限",
        ));
    }
    let ident = id(&value["id"])?;
    let title = str_field(value, "title");
    if title.trim().is_empty() {
        return Err(ApiError::bad("Pixiv 返回的作品数据不完整"));
    }
    let mut w = Work::new(
        &format!("{}{ident}", if novel { "novel:" } else { "" }),
        title,
        if novel {
            "novel"
        } else if value["type"] == "manga" {
            "manga"
        } else {
            "illustration"
        },
    )?;
    w.source_url = if novel {
        format!("https://www.pixiv.net/novel/show.php?id={ident}")
    } else {
        format!("https://www.pixiv.net/artworks/{ident}")
    };
    w.author = str_field(&value["user"], "name").into();
    if !value["user"]["id"].is_null() {
        w.author_id = Some(id(&value["user"]["id"])?);
    }
    w.description = plain(str_field(value, "caption"));
    w.rating = if value["x_restrict"].as_i64().unwrap_or(0) > 0 {
        "adult"
    } else {
        "unknown"
    }
    .into();
    w.page_count = value["page_count"].as_i64();
    if !value["page_count"].is_null() && !w.page_count.is_some_and(|n| (1..=1_000_000).contains(&n))
    {
        return Err(ApiError::bad("Pixiv 作品页数无效"));
    }
    w.bookmarks = value["total_bookmarks"].as_i64().filter(|n| *n >= 0);
    w.bookmarked = value["is_bookmarked"].as_bool();
    w.published = timestamp(str_field(value, "create_date"));
    w.tags = value["tags"]
        .as_array()
        .map(|v| {
            v.iter()
                .filter_map(|t| t["name"].as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    if value["series"].is_object() && !value["series"].as_object().unwrap().is_empty() {
        w.series = Some(
            json!({"remote_id":id(&value["series"]["id"])? ,"title":value["series"]["title"],"kind":if novel{"novel"}else{"manga"}}),
        );
    }
    Ok(w)
}
pub fn timestamp(input: &str) -> Option<String> {
    chrono::DateTime::parse_from_rfc3339(input).ok().map(|d| {
        d.with_timezone(&chrono::Utc)
            .naive_utc()
            .format("%Y-%m-%d %H:%M:%S%.6f")
            .to_string()
    })
}
#[derive(Clone)]
pub struct Pixiv {
    state: AppState,
    http: SafeHttp,
    root: String,
    token: Token,
    pub ctx: Option<Context>,
}
impl Pixiv {
    pub async fn connect(s: &AppState, c: &Value, ctx: Option<Context>) -> TaskResult<Self> {
        let refresh = str_field(c, "refresh_token").trim();
        if refresh.is_empty() || refresh.len() > 8192 || refresh.chars().any(char::is_whitespace) {
            return Err(ApiError::bad("Pixiv 需要有效 refresh_token").into());
        }
        let cell = token_cell(refresh)?;
        let mut cached = cell.lock().await;
        let token = if let Some(t) = cached.as_ref().filter(|t| t.expires > Instant::now()) {
            t.clone()
        } else {
            let v = oauth(
                s,
                vec![
                    ("grant_type".into(), "refresh_token".into()),
                    ("refresh_token".into(), refresh.into()),
                ],
                ctx.clone(),
            )
            .await?;
            let access = str_field(&v, "access_token");
            if access.is_empty() || access.len() > 8192 {
                return Err(ApiError::bad("Pixiv 未返回有效登录令牌").into());
            }
            let t = Token {
                access: access.into(),
                user: id(&v["user"]["id"])?,
                expires: Instant::now()
                    + Duration::from_secs(
                        v["expires_in"].as_u64().unwrap_or(3600).clamp(120, 3600) - 60,
                    ),
            };
            *cached = Some(t.clone());
            t
        };
        drop(cached);
        let (http, root) = SafeHttp::api(s, "pixiv", ctx.clone()).await?;
        Ok(Self {
            state: s.clone(),
            http,
            root,
            token,
            ctx,
        })
    }
    pub fn user(&self) -> &str {
        &self.token.user
    }
    fn auth_headers(&self) -> Result<HeaderMap> {
        let mut h = headers();
        h.insert(
            AUTHORIZATION,
            sensitive(&format!("Bearer {}", self.token.access))?,
        );
        Ok(h)
    }
    pub async fn request(&self, path: &str, params: &[(&str, String)]) -> TaskResult<Value> {
        if !path.starts_with('/') || path.contains(['?', '#']) {
            return Err(ApiError::bad("Pixiv API 路径无效").into());
        }
        let mut u = url::Url::parse(&format!("{}{path}", self.root.trim_end_matches('/')))
            .map_err(|_| ApiError::bad("Pixiv API 地址无效"))?;
        u.query_pairs_mut()
            .extend_pairs(params.iter().map(|(k, v)| (*k, v.as_str())));
        let v = self
            .http
            .json(
                Method::GET,
                u.as_str(),
                self.auth_headers()?,
                RequestBody::Empty,
            )
            .await?;
        if !v.is_object()
            || v.as_object().unwrap().is_empty()
            || v.get("error").is_some_and(|e| !e.is_null() && e != false)
        {
            return Err(ApiError::bad("Pixiv 请求失败：作品不可访问、登录过期或触发限流").into());
        }
        Ok(v)
    }
    async fn checkpoint(&self, p: f64) -> TaskResult<()> {
        if let Some(c) = &self.ctx {
            c.checkpoint(Some(p)).await?;
        }
        Ok(())
    }
    pub async fn detail(&self, id: &str, novel: bool) -> TaskResult<Value> {
        positive(id)?;
        let (path, key, param) = if novel {
            ("/v2/novel/detail", "novel", "novel_id")
        } else {
            ("/v1/illust/detail", "illust", "illust_id")
        };
        let v = self.request(path, &[(param, id.into())]).await?;
        let value = v.get(key).cloned().unwrap_or(v);
        if super::id(&value["id"])? != id {
            return Err(ApiError::bad("Pixiv 返回的作品编号与请求不一致").into());
        }
        Ok(value)
    }
    pub async fn novel_body(&self, ident: &str) -> TaskResult<Value> {
        positive(ident)?;
        let mut u = url::Url::parse(&format!(
            "{}/webview/v2/novel",
            self.root.trim_end_matches('/')
        ))
        .unwrap();
        u.query_pairs_mut()
            .extend_pairs([("id", ident), ("viewer_version", "20221031_ai")]);
        let data = self
            .http
            .bytes(
                Method::GET,
                u.as_str(),
                self.auth_headers()?,
                RequestBody::Empty,
                20 * 1024 * 1024,
            )
            .await?;
        let text = std::str::from_utf8(&data)
            .map_err(|_| ApiError::bad("Pixiv 小说正文不是有效 UTF-8"))?;
        extract_novel(text).map_err(Into::into)
    }
    pub async fn fetch(
        &self,
        remote: &str,
        download: bool,
        include_preview: bool,
    ) -> TaskResult<Work> {
        let remote = normalize(remote)?;
        let novel = remote.starts_with("novel:");
        let ident = remote.trim_start_matches("novel:");
        let value = self.detail(ident, novel).await?;
        if download && value["type"] == "ugoira" {
            return Err(ApiError::bad("动图暂不支持完整帧序列离线保存").into());
        }
        let mut work = metadata(&value, novel)?;
        self.checkpoint(10.0).await?;
        if include_preview && (!download || novel) {
            let images = &value["image_urls"];
            let url = ["medium", "large", "square_medium"]
                .iter()
                .filter_map(|k| images[*k].as_str())
                .find(|s| !s.is_empty())
                .unwrap_or("");
            work.preview = preview(
                &self.state,
                url,
                &["pximg.net"],
                image_headers(),
                self.ctx.clone(),
            )
            .await?;
        }
        if !download {
            return Ok(work);
        }
        let image_http =
            SafeHttp::new(&self.state, vec!["pximg.net".into()], self.ctx.clone()).await?;
        if novel {
            let body = self.novel_body(ident).await?;
            if !body["id"].is_null() && id(&body["id"])? != ident {
                return Err(ApiError::bad("Pixiv 小说正文编号与请求不一致").into());
            }
            let text = body["text"]
                .as_str()
                .or_else(|| body["novel_text"].as_str())
                .or_else(|| body["novel"]["text"].as_str())
                .filter(|s| !s.is_empty())
                .ok_or_else(|| ApiError::bad("Pixiv 小说正文格式已变化，未保存空正文"))?;
            if text.len() > 16 * 1024 * 1024 {
                return Err(ApiError::bad("小说正文超过 16 MiB 限制").into());
            }
            work.files
                .bytes(&format!("{ident}.txt"), text.as_bytes(), None)
                .await?;
            self.novel_images(&body, text, &mut work, &HashSet::new())
                .await?;
        } else {
            let urls = originals(&value)?;
            for (index, url) in urls.iter().enumerate() {
                self.checkpoint(10.0 + 80.0 * index as f64 / urls.len() as f64)
                    .await?;
                let ext = suffix(url)?;
                let response = image_http
                    .send(Method::GET, url, image_headers(), RequestBody::Empty)
                    .await?;
                work.files
                    .response(
                        &format!("{:05}.{ext}", index + 1),
                        response,
                        None,
                        self.ctx.as_ref(),
                    )
                    .await?;
            }
        }
        Ok(work)
    }
    pub async fn novel_images(
        &self,
        body: &Value,
        text: &str,
        work: &mut Work,
        existing: &HashSet<String>,
    ) -> TaskResult<()> {
        let keys = image_keys(text);
        let mut artworks: HashMap<String, Vec<String>> = HashMap::new();
        let http = SafeHttp::new(&self.state, vec!["pximg.net".into()], self.ctx.clone()).await?;
        for (index, key) in keys.iter().enumerate() {
            if existing.contains(key) {
                continue;
            }
            self.checkpoint(15.0 + 70.0 * index as f64 / keys.len().max(1) as f64)
                .await?;
            let (kind, ident) = key.split_once(':').unwrap();
            let url = if kind == "uploadedimage" {
                body["images"][ident]["urls"]["original"]
                    .as_str()
                    .ok_or_else(|| ApiError::bad("小说正文插图不可用，请在重试队列重试"))?
                    .to_owned()
            } else {
                let (ident, page) = ident.split_once('-').unwrap_or((ident, "1"));
                let page = page
                    .parse::<usize>()
                    .map_err(|_| ApiError::bad("小说插图页码无效"))?;
                if !artworks.contains_key(ident) {
                    artworks.insert(ident.into(), originals(&self.detail(ident, false).await?)?);
                }
                artworks[ident]
                    .get(page - 1)
                    .cloned()
                    .ok_or_else(|| ApiError::bad("小说引用的 Pixiv 插画页码不可用"))?
            };
            let ext = suffix(&url)?;
            let response = http
                .send(Method::GET, &url, image_headers(), RequestBody::Empty)
                .await?;
            work.files
                .response(
                    &format!("novel-{}.{ext}", key.replace(':', "-")),
                    response,
                    Some(key.clone()),
                    self.ctx.as_ref(),
                )
                .await?;
        }
        Ok(())
    }
}
pub fn image_headers() -> HeaderMap {
    let mut h = HeaderMap::new();
    h.insert(REFERER, HeaderValue::from_static("https://www.pixiv.net/"));
    h
}
pub fn originals(v: &Value) -> Result<Vec<String>> {
    let mut urls = vec![];
    if let Some(pages) = v["meta_pages"].as_array() {
        for p in pages {
            urls.push(
                p["image_urls"]["original"]
                    .as_str()
                    .unwrap_or("")
                    .to_owned(),
            );
        }
    }
    if urls.is_empty() && v["page_count"] == 1 {
        urls.push(
            v["meta_single_page"]["original_image_url"]
                .as_str()
                .unwrap_or("")
                .into(),
        );
    }
    if urls.is_empty()
        || urls.len() > 2000
        || v["page_count"].as_u64() != Some(urls.len() as u64)
        || urls.iter().any(String::is_empty)
        || urls.iter().collect::<HashSet<_>>().len() != urls.len()
    {
        return Err(ApiError::bad(
            "Pixiv 原图目录与作品页数不一致，未保存不完整作品",
        ));
    }
    for u in &urls {
        network::validate_url(u, &["pximg.net".into()])?;
        suffix(u)?;
    }
    Ok(urls)
}
pub fn extract_novel(input: &str) -> Result<Value> {
    // Parse one JSON value instead of greedy regex across untrusted JavaScript.
    for (at, _) in input.match_indices("novel:") {
        let text = input[at + 6..].trim_start();
        if !text.starts_with('{') {
            continue;
        }
        let mut stream = serde_json::Deserializer::from_str(text).into_iter::<Value>();
        if let Some(Ok(v)) = stream.next() {
            let rest = &text[stream.byte_offset()..];
            if rest
                .trim_start()
                .strip_prefix(',')
                .is_some_and(|r| r.trim_start().starts_with("isOwnWork"))
            {
                return Ok(v);
            }
        }
    }
    Err(ApiError::bad("Pixiv 小说正文格式已变化，请更新适配器"))
}
pub fn image_keys(text: &str) -> Vec<String> {
    let re = regex::Regex::new(
        r"\[((?:uploadedimage:[1-9]\d{0,19}|pixivimage:[1-9]\d{0,19}(?:-[1-9]\d{0,4})?))\]",
    )
    .unwrap();
    let mut seen = HashSet::new();
    re.captures_iter(text)
        .filter_map(|c| {
            let key = c[1].to_owned();
            if seen.insert(key.clone()) {
                Some(key)
            } else {
                None
            }
        })
        .collect()
}
