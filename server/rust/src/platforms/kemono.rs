use super::{preview, str_field};
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
    header::{ACCEPT, HeaderMap, HeaderValue, REFERER, USER_AGENT},
};
use serde_json::Value;
use std::collections::HashSet;
pub const SERVICES: [&str; 8] = [
    "patreon",
    "fanbox",
    "fantia",
    "afdian",
    "boosty",
    "gumroad",
    "subscribestar",
    "dlsite",
];
fn part(v: &str) -> bool {
    !v.is_empty()
        && v.len() <= 128
        && v.bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}
fn parts(v: &[&str]) -> Result<String> {
    if v.len() < 2 || !SERVICES.contains(&v[0]) || v[1..].iter().any(|v| !part(v)) {
        return Err(ApiError::bad("Kemono 编号格式无效"));
    }
    let id = v.join(":");
    if id.len() > 191 {
        return Err(ApiError::bad("Kemono 作品编号过长"));
    }
    Ok(id)
}
pub fn normalize(value: &str, author: bool) -> Result<String> {
    let value = value.trim();
    let chunks: Vec<_> = value.split(':').collect();
    let count = if author { 2 } else { 3 };
    if chunks.len() == count && !value.contains('/') {
        return parts(&chunks);
    }
    let u = network::validate_url(value, &["kemono.cr".into()])?;
    if !["kemono.cr", "www.kemono.cr"].contains(&u.host_str().unwrap_or("")) {
        return Err(ApiError::bad("请输入 Kemono 官方作品或作者链接"));
    }
    let p: Vec<_> = u.path().trim_matches('/').split('/').collect();
    if author && p.len() == 3 && p[1] == "user" {
        parts(&[p[0], p[2]])
    } else if !author && p.len() == 5 && p[1] == "user" && p[3] == "post" {
        parts(&[p[0], p[2], p[4]])
    } else {
        Err(ApiError::bad("请输入 Kemono 官方作品或作者链接"))
    }
}
pub fn media_path(path: &str) -> Result<&str> {
    if path.len() > 1024
        || !path.starts_with('/')
        || path.starts_with("//")
        || path.split('/').any(|v| v == "." || v == "..")
        || !path
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_./-".contains(&c))
    {
        return Err(ApiError::bad("Kemono 文件路径格式无效"));
    }
    Ok(path)
}
fn inline(src: &str) -> Result<String> {
    if let Some(path) = src.strip_prefix("/data/") {
        return Ok(media_path(&format!("/{path}"))?.into());
    }
    let src = if src.starts_with("//") {
        format!("https:{src}")
    } else {
        src.into()
    };
    let u = network::validate_url(&src, &["kemono.cr".into()])?;
    if u.query().is_some() || u.fragment().is_some() || !u.path().starts_with("/data/") {
        return Err(ApiError::bad(
            "Kemono 正文包含外部媒体，暂不支持完整离线保存",
        ));
    }
    Ok(media_path(&u.path()[5..])?.into())
}
pub struct Document {
    pub text: String,
    pub media: Vec<String>,
    pub unsupported: bool,
}
pub fn document(input: &str) -> Document {
    let doc = scraper::Html::parse_fragment(input);
    let mut text = String::new();
    let mut media = vec![];
    let mut unsupported = false;
    for node in doc.root_element().descendants() {
        let hidden = node.ancestors().any(|n| {
            n.value()
                .as_element()
                .is_some_and(|e| ["script", "style", "template"].contains(&e.name()))
        });
        if hidden {
            continue;
        }
        match node.value() {
            scraper::Node::Text(t) => text.push_str(t),
            scraper::Node::Element(e) => {
                let name = e.name();
                if [
                    "p",
                    "div",
                    "section",
                    "article",
                    "li",
                    "ul",
                    "ol",
                    "blockquote",
                    "pre",
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    "h5",
                    "h6",
                    "tr",
                ]
                .contains(&name)
                {
                    text.push_str("\n\n");
                } else if name == "br" {
                    text.push('\n');
                }
                if ["iframe", "audio", "object", "embed"].contains(&name) {
                    unsupported = true;
                }
                if ["img", "video", "source"].contains(&name) {
                    let src = e.attr("src").or_else(|| e.attr("data-src"));
                    if let Some(src) = src {
                        media.push(src.into());
                    } else if name == "img"
                        || name == "video"
                            && scraper::ElementRef::wrap(node).is_none_or(|e| {
                                e.select(&scraper::Selector::parse("source[src]").unwrap())
                                    .next()
                                    .is_none()
                            })
                    {
                        unsupported = true;
                    }
                }
            }
            _ => (),
        }
    }
    let text = text.replace(['\r', '\0'], "");
    let re = regex::Regex::new(r"\n[ \t]*\n(?:[ \t]*\n)+").unwrap();
    Document {
        text: re.replace_all(&text, "\n\n").trim().into(),
        media,
        unsupported,
    }
}
#[derive(Clone)]
pub struct Kemono {
    state: AppState,
    http: SafeHttp,
    root: String,
    ctx: Option<Context>,
}
impl Kemono {
    pub async fn connect(s: &AppState, ctx: Option<Context>) -> TaskResult<Self> {
        let (http, root) = SafeHttp::api(s, "kemono", ctx.clone()).await?;
        Ok(Self {
            state: s.clone(),
            http,
            root,
            ctx,
        })
    }
    pub async fn request(&self, path: &str) -> TaskResult<Value> {
        let mut h = HeaderMap::new();
        h.insert(ACCEPT, HeaderValue::from_static("text/css"));
        h.insert(USER_AGENT, HeaderValue::from_static("QiyeLibrary/0.1"));
        let v = self
            .http
            .json(
                Method::GET,
                &format!("{}/{}", self.root.trim_end_matches('/'), path),
                h,
                RequestBody::Empty,
            )
            .await?;
        if !v.is_object() || v.get("error").is_some_and(|e| !e.is_null() && e != false) {
            return Err(ApiError::bad("Kemono 返回的数据格式无效或作品不可访问").into());
        }
        Ok(v)
    }
    pub async fn fetch_author(&self, target: &str) -> TaskResult<Value> {
        let ident = normalize(target, true)?;
        let p: Vec<_> = ident.split(':').collect();
        let v = self
            .request(&format!("{}/user/{}/profile", p[0], p[1]))
            .await?;
        if str_field(&v, "service") != p[0]
            || str_field(&v, "id") != p[1]
            || str_field(&v, "name").trim().is_empty()
        {
            return Err(ApiError::bad("Kemono 作者资料不完整或编号不一致").into());
        }
        Ok(v)
    }
    pub async fn fetch(&self, input: &str, download: bool) -> TaskResult<Work> {
        let ident = normalize(input, false)?;
        let p: Vec<_> = ident.split(':').collect();
        let response = self
            .request(&format!("{}/user/{}/post/{}", p[0], p[1], p[2]))
            .await?;
        let post = &response["post"];
        if str_field(post, "service") != p[0]
            || str_field(post, "user") != p[1]
            || str_field(post, "id") != p[2]
            || !post["title"].is_string()
        {
            return Err(ApiError::bad("Kemono 作品详情不完整或编号不一致").into());
        }
        let doc = document(str_field(post, "content"));
        let mut unsupported = doc.unsupported
            || post
                .get("embed")
                .is_some_and(|v| !v.is_null() && v != false && v != "");
        let mut files = vec![];
        let mut seen = HashSet::new();
        let attachments = post["attachments"]
            .as_array()
            .ok_or_else(|| ApiError::bad("Kemono 文件目录不完整"))?;
        let mut raw = attachments.clone();
        if post["file"].get("path").is_some() {
            raw.insert(0, post["file"].clone());
        }
        for key in ["attachments", "videos"] {
            if let Some(values) = response[key].as_array() {
                raw.extend(values.iter().cloned());
            } else if !response[key].is_null() {
                return Err(ApiError::bad("Kemono 附件目录不完整").into());
            }
        }
        let mut paths = vec![];
        for f in raw {
            paths.push(media_path(str_field(&f, "path"))?.to_owned());
        }
        for src in doc.media {
            match inline(&src) {
                Ok(p) => paths.push(p),
                Err(_) => unsupported = true,
            }
        }
        for path in paths {
            let ext = std::path::Path::new(&path)
                .extension()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_ascii_lowercase();
            if ![
                "jpg", "jpeg", "png", "webp", "gif", "bmp", "mp4", "webm", "m4v", "mov",
            ]
            .contains(&ext.as_str())
            {
                unsupported = true;
                continue;
            }
            if seen.insert(path.clone()) {
                files.push((path, ext));
            }
        }
        if files.len() + usize::from(!doc.text.is_empty()) > 2000 {
            return Err(ApiError::bad("Kemono 作品超过 2000 个文件限制").into());
        }
        if download && unsupported {
            return Err(ApiError::bad(
                "Kemono 作品含未支持的附件或外部媒体，可先保存详情，未标记为已下载",
            )
            .into());
        }
        let author = self.fetch_author(&format!("{}:{}", p[0], p[1])).await?;
        let kind = if !doc.text.is_empty()
            || files
                .iter()
                .any(|(_, e)| ["mp4", "webm", "m4v", "mov"].contains(&e.as_str()))
        {
            "article"
        } else if files.len() == 1 {
            "illustration"
        } else {
            "manga"
        };
        let title = str_field(post, "title").trim();
        let mut w = Work::new(
            &ident,
            &if title.is_empty() {
                format!("Kemono {}", p[2])
            } else {
                title.chars().take(512).collect()
            },
            kind,
        )?;
        w.source_url = format!("https://kemono.cr/{}/user/{}/post/{}", p[0], p[1], p[2]);
        w.author = str_field(&author, "name").into();
        w.author_id = Some(format!("{}:{}", p[0], p[1]));
        w.description = doc.text.chars().take(20000).collect();
        w.tags = vec![format!("服务:{}", p[0])];
        if let Some(tags) = post["tags"].as_array() {
            for t in tags {
                w.tags.push(
                    t.as_str()
                        .ok_or_else(|| ApiError::bad("Kemono 标签格式无效"))?
                        .chars()
                        .take(191)
                        .collect(),
                );
            }
        }
        w.page_count = Some(files.len() as i64 + i64::from(!doc.text.is_empty()));
        let published = str_field(post, "published");
        w.published = super::pixiv::timestamp(published)
            .or_else(|| super::pixiv::timestamp(&format!("{published}Z")));
        if !download {
            if let Some((p, _)) = files
                .iter()
                .find(|(_, e)| !["mp4", "webm", "m4v", "mov"].contains(&e.as_str()))
            {
                w.preview = preview(
                    &self.state,
                    &format!("https://img.kemono.cr/thumbnail/data{p}"),
                    &["kemono.cr"],
                    HeaderMap::new(),
                    self.ctx.clone(),
                )
                .await?;
            }
            return Ok(w);
        }
        if files.is_empty() && doc.text.is_empty() {
            return Err(ApiError::bad("Kemono 作品没有可保存的正文").into());
        }
        let http = SafeHttp::new(&self.state, vec!["kemono.cr".into()], self.ctx.clone()).await?;
        let mut h = HeaderMap::new();
        h.insert(REFERER, HeaderValue::from_static("https://kemono.cr/"));
        for (i, (path, ext)) in files.iter().enumerate() {
            if let Some(c) = &self.ctx {
                c.checkpoint(Some(10.0 + 80.0 * i as f64 / files.len().max(1) as f64))
                    .await?;
            }
            let r = http
                .send(
                    Method::GET,
                    &format!("https://kemono.cr/data{path}"),
                    h.clone(),
                    RequestBody::Empty,
                )
                .await?;
            w.files
                .response(&format!("{:05}.{ext}", i + 1), r, None, self.ctx.as_ref())
                .await?;
        }
        if !doc.text.is_empty() {
            w.files
                .bytes("zz-content.txt", doc.text.as_bytes(), None)
                .await?;
        }
        Ok(w)
    }
}
