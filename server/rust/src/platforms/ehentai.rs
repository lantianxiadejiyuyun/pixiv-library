use super::{preview, sensitive, str_field, suffix};
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
    header::{COOKIE, HeaderMap, HeaderValue, REFERER, USER_AGENT},
};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet, HashSet};
pub fn normalize(input: &str) -> Result<String> {
    let input = input.trim();
    if regex::Regex::new(r"^[0-9]{1,20}:[a-fA-F0-9]{10}$")
        .unwrap()
        .is_match(input)
    {
        return Ok(input.to_ascii_lowercase());
    }
    let u = network::validate_url(input, &["e-hentai.org".into(), "exhentai.org".into()])?;
    let r = regex::Regex::new(r"^/g/(\d{1,20})/([a-fA-F0-9]{10})/?$").unwrap();
    let c = r
        .captures(u.path())
        .ok_or_else(|| ApiError::bad("请输入包含 gallery token 的完整画廊链接"))?;
    Ok(format!("{}:{}", &c[1], c[2].to_ascii_lowercase()))
}
pub fn document(input: &str) -> (Vec<String>, Option<String>) {
    let doc = scraper::Html::parse_document(input);
    let links = doc
        .select(&scraper::Selector::parse("a[href]").unwrap())
        .filter_map(|e| e.value().attr("href").map(str::to_owned))
        .collect();
    let image = doc
        .select(&scraper::Selector::parse("img#img[src]").unwrap())
        .next()
        .and_then(|e| e.value().attr("src").map(str::to_owned));
    (links, image)
}
pub async fn fetch(
    s: &AppState,
    c: &Value,
    input: &str,
    download: bool,
    ctx: Option<Context>,
) -> TaskResult<Work> {
    let ident = normalize(input)?;
    let (gid, token) = ident.split_once(':').unwrap();
    let gid_num = gid
        .parse::<u64>()
        .map_err(|_| ApiError::bad("画廊编号过长"))?;
    let (api, root) = SafeHttp::api(s, "ehentai", ctx.clone()).await?;
    let response = api
        .json(
            Method::POST,
            &root,
            HeaderMap::new(),
            RequestBody::Json(json!({"method":"gdata","gidlist":[[gid_num,token]],"namespace":1})),
        )
        .await?;
    let data = &response["gmetadata"][0];
    let title = data["title_jpn"]
        .as_str()
        .filter(|s| !s.is_empty())
        .or_else(|| data["title"].as_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| ApiError::bad("E-Hentai 无法读取画廊元数据"))?;
    if data["gid"]
        .as_u64()
        .or_else(|| data["gid"].as_str().and_then(|s| s.parse().ok()))
        .is_some_and(|n| n != gid_num)
        || data
            .get("error")
            .is_some_and(|v| !v.is_null() && v != false)
    {
        return Err(ApiError::bad("画廊元数据无效或编号不一致").into());
    }
    let host = if str_field(c, "igneous").is_empty() {
        "e-hentai.org"
    } else {
        "exhentai.org"
    };
    let gallery_url = format!("https://{host}/g/{gid}/{token}/");
    let mut w = Work::new(&ident, title, "manga")?;
    w.source_url = gallery_url.clone();
    w.tags = data["tags"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_str().map(str::to_owned))
        .collect();
    w.author = w
        .tags
        .iter()
        .filter_map(|t| t.strip_prefix("artist:"))
        .collect::<Vec<_>>()
        .join(", ");
    w.description = format!(
        "来源页数 {} · 上传者 {} · 站点展示清晰度",
        data["filecount"].as_str().unwrap_or("?"),
        str_field(data, "uploader")
    );
    w.page_count = data["filecount"]
        .as_i64()
        .or_else(|| data["filecount"].as_str().and_then(|s| s.parse().ok()));
    let mut h = HeaderMap::new();
    h.insert(REFERER, HeaderValue::from_str(&gallery_url).unwrap());
    h.insert(USER_AGENT, HeaderValue::from_static("QiyeLibrary/0.1"));
    if !download {
        w.preview = preview(
            s,
            str_field(data, "thumb"),
            &["ehgt.org", "hath.network", "e-hentai.org", "exhentai.org"],
            h,
            ctx,
        )
        .await?;
        return Ok(w);
    }
    let count = w
        .page_count
        .filter(|n| (1..=2000).contains(n))
        .ok_or_else(|| ApiError::bad("画廊页数无效或超过 2000 页限制"))?;
    let gallery = SafeHttp::new(s, vec![host.into()], ctx.clone()).await?;
    let media = SafeHttp::new(
        s,
        vec![
            "hath.network".into(),
            "ehgt.org".into(),
            "e-hentai.org".into(),
            "exhentai.org".into(),
        ],
        ctx.clone(),
    )
    .await?;
    let mut gh = h.clone();
    let mut cookies = vec![];
    for key in ["ipb_member_id", "ipb_pass_hash", "igneous"] {
        if let Some(v) = c[key].as_str().filter(|s| !s.is_empty()) {
            if v.contains([';', '\r', '\n']) {
                return Err(ApiError::bad("画廊账号 Cookie 格式无效").into());
            }
            cookies.push(format!("{key}={v}"));
        }
    }
    if !cookies.is_empty() {
        gh.insert(COOKIE, sensitive(&cookies.join("; "))?);
    }
    let base = url::Url::parse(&gallery_url).unwrap();
    let mut pending = BTreeSet::from([0u32]);
    let mut visited = HashSet::new();
    let mut pages: BTreeMap<i64, String> = BTreeMap::new();
    let image_re = regex::Regex::new(&format!(r"^/s/[a-fA-F0-9]+/{gid}-(\d+)$")).unwrap();
    while let Some(p) = pending.pop_first() {
        if !visited.insert(p) {
            continue;
        }
        if p > 100 || visited.len() > 100 {
            return Err(ApiError::bad("画廊目录超过 100 页限制，未保存不完整作品").into());
        }
        let raw = gallery
            .bytes(
                Method::GET,
                &format!("{gallery_url}?p={p}"),
                gh.clone(),
                RequestBody::Empty,
                4 * 1024 * 1024,
            )
            .await?;
        let (links, _) = document(&String::from_utf8_lossy(&raw));
        for link in links {
            let u = base
                .join(&link)
                .map_err(|_| ApiError::bad("画廊链接无效"))?;
            if let Some(c) = image_re.captures(u.path()) {
                network::validate_url(u.as_str(), &[host.into()])?;
                let n = c[1].parse::<i64>().unwrap_or(0);
                if (1..=count).contains(&n) {
                    if pages.get(&n).is_some_and(|old| old != u.as_str()) {
                        return Err(ApiError::bad("画廊目录包含冲突页码").into());
                    }
                    pages.insert(n, u.to_string());
                }
            } else if u.path().trim_end_matches('/') == format!("/g/{gid}/{token}") {
                network::validate_url(u.as_str(), &[host.into()])?;
                if let Some((_, p)) = u.query_pairs().find(|(k, _)| k == "p")
                    && let Ok(p) = p.parse::<u32>()
                    && !visited.contains(&p)
                {
                    pending.insert(p);
                }
            }
        }
    }
    if pages.len() != count as usize {
        return Err(ApiError::bad("画廊目录缺页或需要站点验证，未保存不完整作品").into());
    }
    for number in 1..=count {
        if let Some(c) = &ctx {
            c.checkpoint(Some(15.0 + 75.0 * (number - 1) as f64 / count as f64))
                .await?;
        }
        let page = &pages[&number];
        let raw = gallery
            .bytes(
                Method::GET,
                page,
                gh.clone(),
                RequestBody::Empty,
                4 * 1024 * 1024,
            )
            .await?;
        let (_, image) = document(&String::from_utf8_lossy(&raw));
        let image = image
            .ok_or_else(|| ApiError::bad("画廊页面没有可用图片，可能触发限额或站点结构变化"))?;
        let url = url::Url::parse(page)
            .unwrap()
            .join(&image)
            .map_err(|_| ApiError::bad("画廊图片链接无效"))?;
        if url.path().ends_with("509.gif") || url.path().ends_with("509s.gif") {
            return Err(ApiError::bad("E-Hentai 图片配额已用尽，请稍后重试").into());
        }
        let ext = suffix(url.as_str())?;
        let response = media
            .send(Method::GET, url.as_str(), h.clone(), RequestBody::Empty)
            .await?;
        w.files
            .response(&format!("{number:05}.{ext}"), response, None, ctx.as_ref())
            .await?;
    }
    Ok(w)
}
