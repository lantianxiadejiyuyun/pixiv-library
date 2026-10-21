//! Published PicACG signing protocol, independent of Python SDKs.
use super::{preview, sensitive, str_field};
use crate::{
    AppState,
    content_store::Work,
    error::{ApiError, Result},
    network,
    tasks::{Context, TaskResult},
    transport::{RequestBody, SafeHttp},
};
use base64::Engine;
use hmac::{Hmac, Mac};
use reqwest::{
    Method,
    header::{AUTHORIZATION, HeaderMap, HeaderValue, USER_AGENT},
};
use serde_json::{Value, json};
use sha2::Sha256;
use std::collections::HashSet;
const API_KEY: &str = "C69BAF41DA5ABD1FFEDC6D2FEA56B";
const SIGNATURE_KEY: &str = r"~d}$Q7$eIni=V)9\RK/P.RM4;9[7|@/CA}b~OW!3?EV`:<>M7pddUBL5n|0/*Cn";
pub fn signed(
    endpoint: &str,
    method: &str,
    token: &str,
    time: &str,
    nonce: &str,
) -> Result<HeaderMap> {
    let text = format!(
        "{}{time}{nonce}{}{API_KEY}",
        endpoint.trim_start_matches('/'),
        method.to_uppercase()
    )
    .to_lowercase();
    let mut mac = Hmac::<Sha256>::new_from_slice(SIGNATURE_KEY.as_bytes()).unwrap();
    mac.update(text.as_bytes());
    let signature = mac
        .finalize()
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    let mut h = HeaderMap::new();
    for (key, value) in [
        ("api-key", API_KEY),
        ("accept", "application/vnd.picacomic.com.v1+json"),
        ("app-channel", "2"),
        ("app-version", "2.2.1.2.3.3"),
        ("app-uuid", "defaultUuid"),
        ("app-platform", "android"),
        ("app-build-version", "44"),
        ("user-agent", "okhttp/3.8.1"),
        ("image-quality", "original"),
        ("content-type", "application/json; charset=UTF-8"),
        ("time", time),
        ("nonce", nonce),
        ("signature", &signature),
    ] {
        h.insert(
            reqwest::header::HeaderName::from_static(key),
            HeaderValue::from_str(value).map_err(|_| ApiError::bad("哔咔请求字段无效"))?,
        );
    }
    if !token.is_empty() {
        h.insert(AUTHORIZATION, sensitive(token)?);
    }
    Ok(h)
}
pub fn normalize(input: &str) -> Result<String> {
    let input = input.trim();
    let re = regex::Regex::new(r"^[a-fA-F0-9]{24}$").unwrap();
    if re.is_match(input) {
        return Ok(input.to_ascii_lowercase());
    }
    let u = network::validate_url(input, &["picacomic.com".into()])?;
    let c = regex::Regex::new(r"^/comics/([a-fA-F0-9]{24})/?$")
        .unwrap()
        .captures(u.path())
        .ok_or_else(|| ApiError::bad("请输入哔咔 24 位漫画 ID 或作品链接"))?;
    Ok(c[1].to_ascii_lowercase())
}
pub fn media_url(v: &Value) -> Result<String> {
    let path = str_field(v, "path");
    let server = v["fileServer"].as_str().unwrap_or("");
    if path.is_empty() || path.split('/').any(|p| p == "..") || path.contains('\\') {
        return Err(ApiError::bad("哔咔图片路径无效"));
    }
    let root = server.trim_end_matches('/');
    let url = if let Some(rest) = path.strip_prefix("tobeimg/") {
        if [
            "https://storage1.picacomic.com",
            "https://storage-b.picacomic.com",
        ]
        .contains(&root)
        {
            let stem = std::path::Path::new(rest)
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("");
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(format!("{stem}{}", "=".repeat((4 - stem.len() % 4) % 4)))
                .map_err(|_| ApiError::bad("哔咔图片链接解码失败"))?;
            String::from_utf8(bytes).map_err(|_| ApiError::bad("哔咔图片链接无效"))?
        } else {
            format!(
                "{}/{rest}",
                if root.is_empty() {
                    "https://img.picacomic.com"
                } else {
                    root
                }
            )
        }
    } else if let Some(rest) = path.strip_prefix("tobs/") {
        format!(
            "{}/static/{rest}",
            if root.is_empty() {
                "https://storage-b.picacomic.com"
            } else {
                root
            }
        )
    } else {
        format!(
            "{}/static/{}",
            if root.is_empty() {
                "https://storage1.picacomic.com"
            } else {
                root
            },
            path.trim_start_matches('/')
        )
    };
    network::validate_url(&url, &["picacomic.com".into()])?;
    Ok(url)
}
#[derive(Clone)]
pub struct Picacg {
    state: AppState,
    http: SafeHttp,
    root: String,
    token: String,
    ctx: Option<Context>,
}
impl Picacg {
    pub fn credentials(&self) -> Value {
        json!({"token":self.token})
    }
    pub async fn connect(s: &AppState, c: &Value, ctx: Option<Context>) -> TaskResult<Self> {
        let (http, root) = SafeHttp::api(s, "picacg", ctx.clone()).await?;
        let mut p = Self {
            state: s.clone(),
            http,
            root,
            token: str_field(c, "token").into(),
            ctx,
        };
        if p.token.is_empty() {
            let email = str_field(c, "email");
            let password = str_field(c, "password");
            if email.trim().is_empty() || password.is_empty() {
                return Err(ApiError::bad("哔咔需要 token，或邮箱与密码").into());
            }
            let v = p
                .request(
                    "auth/sign-in",
                    Method::POST,
                    Some(json!({"email":email,"password":password})),
                )
                .await?;
            p.token = str_field(&v, "token").into();
            if p.token.is_empty() {
                return Err(ApiError::bad("哔咔登录未返回有效 token").into());
            }
        }
        Ok(p)
    }
    pub async fn request(
        &self,
        path: &str,
        method: Method,
        body: Option<Value>,
    ) -> TaskResult<Value> {
        if path.starts_with('/') || path.contains('#') {
            return Err(ApiError::bad("哔咔接口路径无效").into());
        }
        let h = signed(
            path,
            method.as_str(),
            &self.token,
            &chrono::Utc::now().timestamp().to_string(),
            &uuid::Uuid::new_v4().simple().to_string(),
        )?;
        let v = self
            .http
            .json(
                method,
                &format!("{}/{}", self.root.trim_end_matches('/'), path),
                h,
                body.map(RequestBody::Json).unwrap_or(RequestBody::Empty),
            )
            .await?;
        if ![200, 201].contains(&v["code"].as_u64().unwrap_or(0)) || !v["data"].is_object() {
            return Err(ApiError::bad("哔咔请求被拒绝，请检查账号权限或更新适配器").into());
        }
        Ok(v["data"].clone())
    }
    async fn progress(&self, p: f64) -> TaskResult<()> {
        if let Some(c) = &self.ctx {
            c.checkpoint(Some(p)).await?;
        }
        Ok(())
    }
    pub async fn pages(&self, path: &str, key: &str) -> TaskResult<Vec<Value>> {
        let mut out = vec![];
        for page in 1..=100 {
            self.progress((page as f64).min(85.0)).await?;
            let v = self
                .request(
                    &format!(
                        "{path}{}page={page}",
                        if path.contains('?') { "&" } else { "?" }
                    ),
                    Method::GET,
                    None,
                )
                .await?;
            let group = &v[key];
            let docs = group["docs"]
                .as_array()
                .ok_or_else(|| ApiError::bad("哔咔分页响应格式无效"))?;
            let pages = number(&group["pages"]).ok_or_else(|| ApiError::bad("哔咔分页计数缺失"))?;
            let total = number(&group["total"]).ok_or_else(|| ApiError::bad("哔咔分页计数缺失"))?;
            if pages > 100
                || total > 3000
                || out.len() + docs.len() > 3000
                || docs.iter().any(|v| !v.is_object())
            {
                return Err(ApiError::bad("哔咔分页数量或格式不符合限制").into());
            }
            out.extend(docs.iter().cloned());
            if page >= pages {
                if out.len() as u64 != total {
                    return Err(ApiError::bad("哔咔分页数量不匹配，未保存不完整内容").into());
                }
                return Ok(out);
            }
            if docs.is_empty() {
                return Err(ApiError::bad("哔咔分页意外为空，未保存不完整内容").into());
            }
        }
        Err(ApiError::bad("哔咔分页超过单次限制").into())
    }
    pub async fn fetch(&self, input: &str, download: bool) -> TaskResult<Work> {
        let ident = normalize(input)?;
        let v = self
            .request(&format!("comics/{ident}"), Method::GET, None)
            .await?;
        let comic = &v["comic"];
        let title = comic["title"]
            .as_str()
            .ok_or_else(|| ApiError::bad("哔咔未返回有效作品详情"))?;
        let mut w = Work::new(&ident, title, "manga")?;
        w.source_url = format!("https://picaapi.picacomic.com/comics/{ident}");
        w.author = str_field(comic, "author").into();
        w.description = str_field(comic, "description").into();
        w.tags = comic["tags"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|v| v.as_str().map(str::to_owned))
            .chain(
                comic["categories"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|v| v.as_str().map(|s| format!("分类:{s}"))),
            )
            .collect();
        w.page_count = number(&comic["pagesCount"])
            .filter(|n| (1..=1_000_000).contains(n))
            .map(|n| n as i64);
        if !download {
            let url = media_url(&comic["thumb"]).unwrap_or_default();
            w.preview = preview(
                &self.state,
                &url,
                &["picacomic.com"],
                image_headers(),
                self.ctx.clone(),
            )
            .await?;
            return Ok(w);
        }
        if comic["allowDownload"] == false {
            return Err(ApiError::bad("该作品在平台上不允许下载，可仅保存元数据").into());
        }
        let mut episodes = self.pages(&format!("comics/{ident}/eps"), "eps").await?;
        if episodes.is_empty()
            || episodes
                .iter()
                .any(|v| number(&v["order"]).is_none_or(|n| n > 100000))
        {
            return Err(ApiError::bad("哔咔章节为空或顺序无效").into());
        }
        episodes.sort_by_key(|v| number(&v["order"]).unwrap());
        if episodes
            .iter()
            .filter_map(|v| number(&v["order"]))
            .collect::<HashSet<_>>()
            .len()
            != episodes.len()
        {
            return Err(ApiError::bad("哔咔章节目录重复").into());
        }
        let http =
            SafeHttp::new(&self.state, vec!["picacomic.com".into()], self.ctx.clone()).await?;
        for (chapter, e) in episodes.iter().enumerate() {
            let order = number(&e["order"]).unwrap();
            let pages = self
                .pages(&format!("comics/{ident}/order/{order}/pages"), "pages")
                .await?;
            if pages.is_empty() {
                return Err(ApiError::bad("哔咔章节无可下载页面").into());
            }
            for (i, p) in pages.iter().enumerate() {
                self.progress(
                    10.0 + 80.0 * (chapter as f64 + i as f64 / pages.len() as f64)
                        / episodes.len() as f64,
                )
                .await?;
                let url = media_url(&p["media"])?;
                let ext = super::suffix(&url).unwrap_or_else(|_| "jpg".into());
                let response = http
                    .send(Method::GET, &url, image_headers(), RequestBody::Empty)
                    .await?;
                w.files
                    .response(
                        &format!("e{order:05}_p{:05}.{ext}", i + 1),
                        response,
                        None,
                        self.ctx.as_ref(),
                    )
                    .await?;
            }
        }
        if w.page_count
            .is_some_and(|n| n != w.files.entries.len() as i64)
        {
            return Err(ApiError::bad("哔咔作品页数与详情不一致，未保存不完整作品").into());
        }
        Ok(w)
    }
}
fn image_headers() -> HeaderMap {
    let mut h = HeaderMap::new();
    h.insert(USER_AGENT, HeaderValue::from_static("okhttp/3.8.1"));
    h
}
fn number(v: &Value) -> Option<u64> {
    v.as_u64()
        .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
}
