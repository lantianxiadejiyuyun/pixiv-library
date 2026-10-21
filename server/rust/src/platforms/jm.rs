//! Native HTML adapter and bounded strip restoration.
use super::{plain, preview, sensitive, str_field};
use crate::{
    AppState,
    content_store::Work,
    error::{ApiError, Result},
    network,
    tasks::{Context, TaskResult},
    transport::{RequestBody, SafeHttp},
};
use base64::Engine;
use reqwest::{
    Method,
    header::{COOKIE, HeaderMap, HeaderValue, REFERER},
};
use serde_json::Value;
use sha2::Digest;
use std::collections::HashSet;
pub const IMAGE_HOSTS: [&str; 6] = [
    "cdn-msp.jmapiproxy1.cc",
    "cdn-msp.jmapiproxy2.cc",
    "cdn-msp2.jmapiproxy2.cc",
    "cdn-msp3.jmapiproxy2.cc",
    "cdn-msp.jmapinodeudzn.net",
    "cdn-msp3.jmapinodeudzn.net",
];
pub fn normalize(input: &str) -> Result<String> {
    let input = input.trim();
    let numeric = if input.to_ascii_uppercase().starts_with("JM") {
        &input[2..]
    } else {
        input
    };
    if numeric.len() <= 16 && super::positive(numeric).is_ok() {
        return Ok(numeric.into());
    }
    let u = network::validate_url(
        input,
        &[
            "18comic.vip".into(),
            "18comic.org".into(),
            "jmcomic.me".into(),
            "jm-comic.org".into(),
        ],
    )?;
    let r = regex::Regex::new(r"^/album/([1-9]\d{0,15})/?$").unwrap();
    let c = r
        .captures(u.path())
        .ok_or_else(|| ApiError::bad("请输入 JM 作品 ID 或受支持的作品链接"))?;
    Ok(c[1].into())
}
fn capture(input: &str, pattern: &str) -> Result<String> {
    regex::Regex::new(pattern)
        .unwrap()
        .captures(input)
        .map(|c| c[1].to_owned())
        .ok_or_else(|| ApiError::bad("JM 页面资料不完整，可能需要站点验证或更新适配器"))
}
pub fn unpack(html: &str) -> Result<String> {
    if let Ok(data) = capture(html, r#"const html = base64DecodeUtf8\("([^"]+)"\)"#) {
        if data.len() > 8 * 1024 * 1024 {
            return Err(ApiError::bad("JM 页面超过限制"));
        }
        String::from_utf8(
            base64::engine::general_purpose::STANDARD
                .decode(data)
                .map_err(|_| ApiError::bad("JM 页面解码失败"))?,
        )
        .map_err(|_| ApiError::bad("JM 页面不是 UTF-8"))
    } else {
        Ok(html.into())
    }
}
pub fn segments(scramble: u64, id: u64, filename: &str) -> u32 {
    if id < scramble {
        0
    } else if id < 268850 {
        10
    } else {
        let hex = format!("{:x}", md5::Md5::digest(format!("{id}{filename}")));
        let last = hex.as_bytes()[hex.len() - 1] as u32;
        last % if id < 421926 { 10 } else { 8 } * 2 + 2
    }
}
pub fn restore(bytes: &[u8], segments: u32) -> Result<Vec<u8>> {
    use image::GenericImageView;
    let mut r = image::ImageReader::new(std::io::Cursor::new(bytes))
        .with_guessed_format()
        .map_err(|_| ApiError::bad("JM 图片格式无效"))?;
    let dimensions = image::ImageReader::new(std::io::Cursor::new(bytes))
        .with_guessed_format()
        .map_err(|_| ApiError::bad("JM 图片格式无效"))?
        .into_dimensions()
        .map_err(|_| ApiError::bad("JM 图片尺寸无效"))?;
    if dimensions.0 as u64 * dimensions.1 as u64 > 40_000_000 {
        return Err(ApiError::bad("JM 图片像素数超过限制"));
    }
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(16384);
    limits.max_image_height = Some(16384);
    limits.max_alloc = Some(192 * 1024 * 1024);
    r.limits(limits);
    let img = r
        .decode()
        .map_err(|_| ApiError::bad("JM 图片损坏或尺寸超过限制"))?;
    let (width, height) = img.dimensions();
    let decoded = if segments == 0 {
        img.to_rgb8()
    } else {
        if height < segments || segments > 20 {
            return Err(ApiError::bad("JM 图片分割参数无效"));
        }
        let mut out = image::RgbImage::new(width, height);
        let over = height % segments;
        let step = height / segments;
        for i in 0..segments {
            let sy = height - step * (i + 1) - over;
            let dy = step * i + if i == 0 { 0 } else { over };
            let rows = step + if i == 0 { over } else { 0 };
            let tile = img.crop_imm(0, sy, width, rows).to_rgb8();
            image::imageops::replace(&mut out, &tile, 0, dy as i64);
        }
        out
    };
    let mut out = std::io::Cursor::new(vec![]);
    decoded
        .write_to(&mut out, image::ImageFormat::Png)
        .map_err(|_| ApiError::bad("JM 图片还原失败"))?;
    Ok(out.into_inner())
}
pub struct Album {
    pub title: String,
    pub authors: String,
    pub tags: Vec<String>,
    pub description: String,
    pub count: i64,
    pub chapters: Vec<String>,
}
pub fn album(html: &str, ident: &str) -> Result<Album> {
    let html = unpack(html)?;
    let doc = scraper::Html::parse_document(&html);
    let select = |selector: &str| {
        doc.select(&scraper::Selector::parse(selector).unwrap())
            .map(|e| e.text().collect::<String>().trim().to_owned())
            .collect::<Vec<_>>()
    };
    let actual = capture(&html, r#"<span class="number">.*?[:：]JM(\d+)</span>"#)?;
    if actual != ident {
        return Err(ApiError::bad("JM 返回的作品编号与请求不一致"));
    }
    let title = select("#book-name")
        .first()
        .cloned()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| ApiError::bad("JM 作品标题缺失"))?;
    let count = capture(&html, r#"<span class="pagecount">.*?[:：]\s*(\d+)</span>"#)?
        .parse::<i64>()
        .map_err(|_| ApiError::bad("JM 页数无效"))?;
    if !(1..=2000).contains(&count) {
        return Err(ApiError::bad("JM 作品超过 2000 页或无有效页数"));
    }
    let mut chapters = vec![];
    let mut seen = HashSet::new();
    for e in doc.select(&scraper::Selector::parse("[data-album]").unwrap()) {
        let id = e.value().attr("data-album").unwrap();
        super::positive(id)?;
        if seen.insert(id.to_owned()) {
            chapters.push(id.into());
        }
    }
    if chapters.is_empty() {
        chapters.push(ident.into());
    }
    let description = capture(&html, r"[叙敘]述：([\s\S]*?)</h2>")
        .map(|s| plain(&s))
        .unwrap_or_default();
    Ok(Album {
        title,
        authors: select("[data-type=author] a").join(", "),
        tags: select("[data-type=tags] a"),
        description,
        count,
        chapters,
    })
}
pub fn photo(html: &str, ident: &str) -> Result<(u64, Vec<String>)> {
    let doc = scraper::Html::parse_document(html);
    let actual = doc
        .select(&scraper::Selector::parse("meta[property='og:url']").unwrap())
        .next()
        .and_then(|e| e.value().attr("content"))
        .and_then(|u| url::Url::parse(u).ok())
        .map(|u| u.path().trim_end_matches('/').to_owned());
    if actual.as_deref() != Some(&format!("/photo/{ident}")) {
        return Err(ApiError::bad("JM 章节编号与请求不一致"));
    }
    let scramble = capture(html, r"var\s+scramble_id\s*=\s*(\d+)\s*;")?
        .parse::<u64>()
        .map_err(|_| ApiError::bad("JM 图片分割参数无效"))?;
    let raw = capture(html, r"var\s+page_arr\s*=\s*(\[[^;]+\])\s*;")?;
    let files: Vec<String> =
        serde_json::from_str(&raw).map_err(|_| ApiError::bad("JM 图片目录无效"))?;
    if files.is_empty()
        || files.len() > 2000
        || files.iter().collect::<HashSet<_>>().len() != files.len()
        || files.iter().any(|s| {
            s.len() > 128
                || !s
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
                || s.contains("..")
        })
    {
        return Err(ApiError::bad("JM 图片目录为空、重复或无效"));
    }
    let domain = doc
        .select(&scraper::Selector::parse("img[src]").unwrap())
        .filter_map(|e| e.value().attr("src"))
        .filter_map(|u| url::Url::parse(u).ok())
        .find(|u| u.path().starts_with("/media/albums/blank"))
        .and_then(|u| u.host_str().map(str::to_owned))
        .unwrap_or_else(|| IMAGE_HOSTS[0].into());
    if !IMAGE_HOSTS.contains(&domain.as_str()) {
        return Err(ApiError::bad("JM 返回未支持的图片 CDN 地址，请更新适配器"));
    }
    Ok((
        scramble,
        files
            .into_iter()
            .map(|f| format!("https://{domain}/media/photos/{ident}/{f}"))
            .collect(),
    ))
}
pub async fn fetch(
    s: &AppState,
    c: &Value,
    input: &str,
    download: bool,
    ctx: Option<Context>,
) -> TaskResult<Work> {
    let ident = normalize(input)?;
    let (http, root) = SafeHttp::api(s, "jm", ctx.clone()).await?;
    let mut h = HeaderMap::new();
    h.insert(REFERER, HeaderValue::from_static("https://18comic.vip/"));
    if !str_field(c, "AVS").is_empty() {
        if str_field(c, "AVS").contains([';', '\r', '\n']) {
            return Err(ApiError::bad("JM AVS 会话格式无效").into());
        }
        h.insert(COOKIE, sensitive(&format!("AVS={}", str_field(c, "AVS")))?);
    }
    let raw = http
        .bytes(
            Method::GET,
            &format!("{root}/album/{ident}"),
            h.clone(),
            RequestBody::Empty,
            8 * 1024 * 1024,
        )
        .await?;
    let a = album(&String::from_utf8_lossy(&raw), &ident)?;
    let mut w = Work::new(&ident, &a.title, "manga")?;
    w.author = a.authors;
    w.tags = a.tags;
    w.description = a.description;
    w.page_count = Some(a.count);
    w.source_url = format!("https://18comic.vip/album/{ident}");
    let mut media_headers = HeaderMap::new();
    media_headers.insert(REFERER, HeaderValue::from_static("https://18comic.vip/"));
    if !download {
        w.preview = preview(
            s,
            &format!("https://{}/media/albums/{ident}_3x4.jpg", IMAGE_HOSTS[0]),
            &IMAGE_HOSTS,
            media_headers,
            ctx,
        )
        .await?;
        return Ok(w);
    }
    let images = SafeHttp::new(
        s,
        IMAGE_HOSTS.iter().map(|s| s.to_string()).collect(),
        ctx.clone(),
    )
    .await?;
    for chapter in a.chapters {
        let raw = http
            .bytes(
                Method::GET,
                &format!("{root}/photo/{chapter}"),
                h.clone(),
                RequestBody::Empty,
                8 * 1024 * 1024,
            )
            .await?;
        let (scramble, urls) = photo(&String::from_utf8_lossy(&raw), &chapter)?;
        for url in urls {
            if w.files.entries.len() >= 2000 {
                return Err(ApiError::bad("JM 作品超过 2000 页限制").into());
            }
            if let Some(c) = &ctx {
                c.checkpoint(Some((10.0 + w.files.entries.len() as f64 / 5.0).min(90.0)))
                    .await?;
            }
            let bytes = images
                .bytes(
                    Method::GET,
                    &url,
                    media_headers.clone(),
                    RequestBody::Empty,
                    32 * 1024 * 1024,
                )
                .await?;
            let u = url::Url::parse(&url).unwrap();
            let name = std::path::Path::new(u.path())
                .file_stem()
                .and_then(|v| v.to_str())
                .unwrap_or("");
            let count = segments(
                scramble,
                chapter
                    .parse()
                    .map_err(|_| ApiError::bad("JM 章节编号无效"))?,
                name,
            );
            let permit = s
                .cpu_limit
                .clone()
                .acquire_owned()
                .await
                .map_err(|_| ApiError::bad("图片处理器已关闭"))?;
            let restored = tokio::task::spawn_blocking(move || {
                let _permit = permit;
                restore(&bytes, count)
            })
            .await
            .map_err(|_| ApiError::bad("JM 图片还原失败"))??;
            let index = w.files.entries.len() + 1;
            w.files
                .bytes(&format!("{index:05}.png"), &restored, None)
                .await?;
        }
    }
    if w.files.entries.len() as i64 != a.count {
        return Err(ApiError::bad("JM 下载页数与详情不一致，未保存不完整作品").into());
    }
    Ok(w)
}
