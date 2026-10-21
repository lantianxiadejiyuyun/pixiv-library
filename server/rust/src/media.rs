use crate::{
    ApiJson, AppState,
    db::{iso, json_column, now},
    error::{ApiError, Result},
    library::require_item,
};
use axum::{
    Json,
    body::Body,
    extract::{Path, State},
    http::{Request, StatusCode, header},
    response::{IntoResponse, Response},
};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    io::{Read, Write},
    path::{Component, PathBuf},
    sync::OnceLock,
};
use tower::ServiceExt;
use tower_http::services::ServeFile;

const MAX_TEXT: usize = 16 * 1024 * 1024;
pub async fn io<T: Send + 'static>(
    state: &AppState,
    f: impl FnOnce() -> Result<T> + Send + 'static,
) -> Result<T> {
    let permit = tokio::time::timeout(
        std::time::Duration::from_secs(2),
        state.fs_limit.clone().acquire_owned(),
    )
    .await
    .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "文件服务繁忙，请稍后重试"))?
    .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "文件服务已关闭"))?;
    let task = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        f()
    });
    tokio::time::timeout(std::time::Duration::from_secs(5), task)
        .await
        .map_err(|_| {
            ApiError(
                StatusCode::SERVICE_UNAVAILABLE,
                "NAS 响应超时，请检查存储连接",
            )
        })?
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "文件服务处理失败"))?
}
fn check_root(root: &std::path::Path) -> Result<PathBuf> {
    let root = root.canonicalize()?;
    if std::fs::read_to_string(root.join(".library-storage"))?.trim() != "qiye-library-v1" {
        return Err(ApiError(
            StatusCode::SERVICE_UNAVAILABLE,
            "书库存储标记缺失，请检查 NAS 挂载",
        ));
    }
    Ok(root)
}
pub fn resolve(root: &std::path::Path, relative: &str) -> Result<PathBuf> {
    let rel = std::path::Path::new(relative);
    if relative.is_empty()
        || relative.contains([':', '\\', '\0'])
        || rel.components().any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err(ApiError::bad("无效的书库文件路径"));
    }
    let root = check_root(root)?;
    let path = root.join(rel).canonicalize()?;
    if !path.starts_with(&root) || path == root {
        return Err(ApiError::bad("无效的书库文件路径"));
    }
    Ok(path)
}
pub async fn storage_ok(state: &AppState) -> bool {
    let root = state.config.storage_root.clone();
    io(state, move || check_root(&root)).await.is_ok()
}
pub async fn health(State(state): State<AppState>) -> Response {
    let db = tokio::time::timeout(
        std::time::Duration::from_secs(3),
        state.db.count("SELECT 1", &[]),
    )
    .await
    .is_ok_and(|v| v.is_ok());
    let storage = storage_ok(&state).await;
    (if db&&storage{StatusCode::OK}else{StatusCode::SERVICE_UNAVAILABLE},Json(json!({"status":if db&&storage{"ok"}else{"degraded"},"database":if db{"ok"}else{"unavailable"},"storage":if storage{"ok"}else{"unavailable"}}))).into_response()
}
pub async fn asset(state: &AppState, id: &str) -> Result<(Value, PathBuf)> {
    let a = state
        .db
        .one("SELECT * FROM assets WHERE id=?", &[json!(id)])
        .await?
        .ok_or_else(|| ApiError::missing("文件不存在"))?;
    let root = state.config.storage_root.clone();
    let rel = a["relative_path"]
        .as_str()
        .ok_or_else(|| ApiError::bad("文件路径无效"))?
        .to_owned();
    let size = a["size_bytes"]
        .as_u64()
        .ok_or_else(|| ApiError::bad("文件大小无效"))?;
    let path = io(state, move || {
        let p = resolve(&root, &rel)?;
        let m = std::fs::metadata(&p)?;
        if !m.is_file() || m.len() != size {
            return Err(ApiError(
                StatusCode::SERVICE_UNAVAILABLE,
                "原文件缺失或存储不可用，请检查 NAS 后重试",
            ));
        }
        Ok(p)
    })
    .await?;
    Ok((a, path))
}
pub(crate) async fn serve(path: PathBuf, mime: &str, request: Request<Body>) -> Result<Response> {
    let safe = if mime.starts_with("image/") && !mime.contains("svg")
        || matches!(
            mime,
            "video/mp4"
                | "video/webm"
                | "video/quicktime"
                | "application/vnd.android.package-archive"
        ) {
        mime
    } else {
        "text/plain; charset=utf-8"
    };
    let mime: safe_mime::Mime = safe.parse().map_err(|_| ApiError::bad("文件类型无效"))?;
    let result = ServeFile::new_with_mime(path, &mime).oneshot(request).await;
    let mut r = match result {
        Ok(r) => r.map(Body::new),
        Err(_) => return Err(ApiError(StatusCode::SERVICE_UNAVAILABLE, "文件读取失败")),
    };
    r.headers_mut().insert(
        "content-security-policy",
        "default-src 'none'; sandbox".parse().unwrap(),
    );
    r.headers_mut()
        .insert(header::X_CONTENT_TYPE_OPTIONS, "nosniff".parse().unwrap());
    Ok(r)
}
pub(crate) async fn blob(
    s: &AppState,
    preview: &Value,
    request: Request<Body>,
) -> Result<Response> {
    if preview["mime_type"] != "image/webp" {
        return Err(ApiError::missing("图片尚未保存"));
    }
    let relative = preview["relative_path"]
        .as_str()
        .ok_or_else(|| ApiError::bad("图片文件路径无效"))?
        .to_owned();
    let size = preview["size_bytes"]
        .as_u64()
        .filter(|n| *n > 0 && *n <= 8 * 1024 * 1024)
        .ok_or_else(|| ApiError::bad("图片文件大小无效"))?;
    let root = s.config.storage_root.clone();
    let path = io(s, move || {
        let p = resolve(&root, &relative)?;
        if !p.is_file() || p.metadata()?.len() != size {
            return Err(ApiError(
                axum::http::StatusCode::SERVICE_UNAVAILABLE,
                "图片文件缺失，请检查 NAS 挂载",
            ));
        }
        Ok(p)
    })
    .await?;
    serve(path, "image/webp", request).await
}
use mime as safe_mime;
pub async fn content(
    State(state): State<AppState>,
    Path(id): Path<String>,
    request: Request<Body>,
) -> Result<Response> {
    let (a, path) = asset(&state, &id).await?;
    serve(
        path,
        a["mime_type"]
            .as_str()
            .unwrap_or("application/octet-stream"),
        request,
    )
    .await
}
pub async fn cover(
    State(state): State<AppState>,
    Path(id): Path<String>,
    request: Request<Body>,
) -> Result<Response> {
    let item = require_item(&state.db, &id).await?;
    let cover = json_column(&item["preview_cover"]);
    if cover["mime_type"] != "image/webp" {
        return Err(ApiError::missing("封面尚未保存"));
    }
    let root = state.config.storage_root.clone();
    let rel = cover["relative_path"]
        .as_str()
        .ok_or_else(|| ApiError::missing("封面不存在"))?
        .to_owned();
    let size = cover["size_bytes"].as_u64();
    let p = io(&state, move || {
        let path = resolve(&root, &rel)?;
        let meta = std::fs::metadata(&path)?;
        if !meta.is_file() || Some(meta.len()) != size {
            return Err(ApiError(
                StatusCode::SERVICE_UNAVAILABLE,
                "封面文件缺失或存储不可用，请检查 NAS 后重试",
            ));
        }
        Ok(path)
    })
    .await?;
    serve(
        p,
        cover["mime_type"].as_str().unwrap_or("image/webp"),
        request,
    )
    .await
}
pub async fn thumbnail(
    State(state): State<AppState>,
    Path(id): Path<String>,
    request: Request<Body>,
) -> Result<Response> {
    let (a, path) = asset(&state, &id).await?;
    if !a["mime_type"]
        .as_str()
        .is_some_and(|m| m.starts_with("image/"))
    {
        return Err(ApiError::bad("该文件不是图片"));
    }
    let digest = a["sha256"]
        .as_str()
        .filter(|s| {
            s.len() == 64
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
        .ok_or_else(|| ApiError::bad("文件摘要无效"))?
        .to_owned();
    let root = state.config.storage_root.clone();
    let rel = format!("thumbnails/{}/{}.webp", &digest[..2], digest);
    // Reuse legacy previews but write only to this process's private temporary
    // cache in validation mode. No NAS or library metadata writes on a GET.
    if let Ok(cached) = io(&state, move || resolve(&root, &rel)).await {
        return serve(cached, "image/webp", request).await;
    }
    let cache_root = state.thumbnail_cache.path().to_owned();
    let target = cache_root.join(format!("{digest}.webp"));
    if tokio::fs::try_exists(&target).await? {
        return serve(target, "image/webp", request).await;
    }
    let permit = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        state.thumbnail_limit.clone().acquire_owned(),
    )
    .await
    .map_err(|_| {
        ApiError(
            StatusCode::SERVICE_UNAVAILABLE,
            "缩略图正在生成，请稍后重试",
        )
    })?
    .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "图片服务已关闭"))?;
    let task = tokio::task::spawn_blocking(move || -> Result<PathBuf> {
        let _permit = permit;
        if target.is_file() {
            return Ok(target);
        }
        use image::ImageDecoder;
        let invalid = || ApiError::bad("图片损坏或像素数超过安全限制");
        let mut reader = image::ImageReader::open(path)?.with_guessed_format()?;
        let mut limits = image::Limits::default();
        limits.max_alloc = Some(128 * 1024 * 1024);
        limits.max_image_width = Some(16384);
        limits.max_image_height = Some(16384);
        reader.limits(limits);
        let mut decoder = reader.into_decoder().map_err(|_| invalid())?;
        let (w, h) = decoder.dimensions();
        if u64::from(w) * u64::from(h) > 40_000_000 {
            return Err(invalid());
        }
        let orientation = decoder.orientation().map_err(|_| invalid())?;
        let mut image = image::DynamicImage::from_decoder(decoder).map_err(|_| invalid())?;
        image.apply_orientation(orientation);
        let thumb = image.thumbnail(480, 640);
        drop(image);
        let mut file = tempfile::NamedTempFile::new_in(&cache_root)?;
        thumb
            .write_to(file.as_file_mut(), image::ImageFormat::WebP)
            .map_err(|_| invalid())?;
        file.flush()?;
        // A private per-process cache, bounded by count and total bytes.
        // No library or NAS writes, and concurrent requests share one result.
        let mut entries = std::fs::read_dir(&cache_root)?
            .filter_map(|r| r.ok())
            .filter_map(|e| {
                let m = e.metadata().ok()?;
                if e.path().extension()?.to_str()? == "webp" {
                    Some((e.path(), m.len(), m.modified().ok()))
                } else {
                    None
                }
            })
            .collect::<Vec<_>>();
        let mut bytes = entries.iter().map(|e| e.1).sum::<u64>();
        let mut count = entries.len();
        entries.sort_by_key(|e| e.2);
        for (path, size, _) in entries {
            if count < 1000 && bytes + file.as_file().metadata()?.len() <= 512 * 1024 * 1024 {
                break;
            }
            if std::fs::remove_file(path).is_ok() {
                bytes = bytes.saturating_sub(size);
                count -= 1;
            }
        }
        file.persist_noclobber(&target).map_err(|e| e.error)?;
        Ok(target)
    });
    let path = tokio::time::timeout(std::time::Duration::from_secs(10), task)
        .await
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "图片处理超时"))?
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "图片处理失败"))??;
    serve(path, "image/webp", request).await
}
#[derive(Serialize)]
pub struct Section {
    index: usize,
    title: String,
    text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    blocks: Option<Vec<Value>>,
}
fn chapter_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"\[newpage\]|\[chapter:([^\[\]\r\n]{1,512})\]").unwrap())
}
fn image_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"\[(uploadedimage:[1-9]\d{0,19}|pixivimage:[1-9]\d{0,19}(?:-[1-9]\d{0,3})?)\]")
            .unwrap()
    })
}
pub fn parse_sections(text: &str, images: Option<&HashMap<String, Value>>) -> Result<Vec<Section>> {
    let mut sections = vec![];
    let mut title = "正文".to_owned();
    let mut start = 0;
    for cap in chapter_re().captures_iter(text) {
        let m = cap.get(0).unwrap();
        let body = &text[start..m.start()];
        if !body.trim().is_empty() {
            sections.push(Section {
                index: sections.len(),
                title: title.clone(),
                text: body.to_owned(),
                blocks: None,
            });
        }
        if sections.len() >= 5000 {
            return Err(ApiError::bad("正文分节超过 5000 节限制"));
        }
        title = cap
            .get(1)
            .map(|v| v.as_str().trim().to_owned())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| format!("第 {} 节", sections.len() + 1));
        start = m.end();
    }
    if !text[start..].trim().is_empty() || sections.is_empty() {
        sections.push(Section {
            index: sections.len(),
            title,
            text: text[start..].to_owned(),
            blocks: None,
        });
    }
    if let Some(images) = images {
        for s in &mut sections {
            let mut blocks = vec![];
            let mut start = 0;
            for c in image_re().captures_iter(&s.text) {
                let m = c.get(0).unwrap();
                if m.start() > start {
                    blocks.push(json!({"type":"text","text":s.text[start..m.start()]}));
                }
                let key = &c[1];
                let mut image = images
                    .get(key)
                    .cloned()
                    .unwrap_or_else(|| json!({"unavailable":true}));
                image["type"] = json!("image");
                image["key"] = json!(key);
                blocks.push(image);
                start = m.end();
            }
            if start < s.text.len() || blocks.is_empty() {
                blocks.push(json!({"type":"text","text":s.text[start..]}));
            }
            s.blocks = Some(blocks);
        }
    }
    Ok(sections)
}
async fn read_text(state: &AppState, path: PathBuf) -> Result<String> {
    io(state, move || {
        let mut b = Vec::new();
        std::fs::File::open(path)?
            .take(MAX_TEXT as u64 + 1)
            .read_to_end(&mut b)?;
        if b.len() > MAX_TEXT {
            return Err(ApiError::bad(
                "文本超过 16 MiB 阅读解析限制，请下载原始文件",
            ));
        }
        String::from_utf8(b)
            .map(|s| s.strip_prefix('\u{feff}').unwrap_or(&s).to_owned())
            .map_err(|_| ApiError::bad("文本不是有效的 UTF-8 内容"))
    })
    .await
}
pub async fn text_sections(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>> {
    let (a, path) = asset(&state, &id).await?;
    if !a["mime_type"]
        .as_str()
        .is_some_and(|s| s.starts_with("text/"))
    {
        return Err(ApiError::bad("仅文本文件支持章节阅读"));
    }
    let mut images = HashMap::new();
    for a in state.db.query("SELECT id,inline_key,width,height FROM assets WHERE item_id=? AND inline_key IS NOT NULL AND status='available' AND mime_type LIKE 'image/%'",&[a["item_id"].clone()]).await?{images.insert(a["inline_key"].as_str().unwrap().to_owned(),json!({"asset_id":a["id"],"url":format!("/api/assets/{}/content",a["id"].as_str().unwrap()),"width":a["width"],"height":a["height"]}));}
    let text = read_text(&state, path).await?;
    Ok(Json(
        json!({"sections":parse_async(&state,text,Some(images)).await?}),
    ))
}
async fn parse_async(
    state: &AppState,
    text: String,
    images: Option<HashMap<String, Value>>,
) -> Result<Vec<Section>> {
    let permit = tokio::time::timeout(
        std::time::Duration::from_secs(2),
        state.cpu_limit.clone().acquire_owned(),
    )
    .await
    .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "正文解析繁忙，请稍后重试"))?
    .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "正文解析服务已关闭"))?;
    let task = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        parse_sections(&text, images.as_ref())
    });
    tokio::time::timeout(std::time::Duration::from_secs(10), task)
        .await
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "正文解析超时"))?
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "正文解析失败"))?
}
#[derive(Deserialize)]
pub struct Progress {
    page: u32,
    #[serde(default)]
    section: u32,
    #[serde(default)]
    offset: f64,
}
pub async fn progress(
    State(state): State<AppState>,
    Path(id): Path<String>,
    ApiJson(v): ApiJson<Progress>,
) -> Result<Json<Value>> {
    state.config.writable()?;
    if !v.offset.is_finite() || !(0.0..=1.0).contains(&v.offset) {
        return Err(ApiError::invalid("阅读偏移须在 0–1 之间"));
    }
    require_item(&state.db, &id).await?;
    let assets=state.db.query("SELECT id,mime_type FROM assets WHERE item_id=? AND inline_key IS NULL ORDER BY position,id",&[json!(id)]).await?;
    let a = assets
        .get(v.page as usize)
        .ok_or_else(|| ApiError::bad("阅读位置超出页数范围"))?;
    if a["mime_type"]
        .as_str()
        .is_some_and(|s| s.starts_with("text/"))
    {
        let (_, p) = asset(&state, a["id"].as_str().unwrap()).await?;
        if v.section as usize
            >= parse_async(&state, read_text(&state, p).await?, None)
                .await?
                .len()
        {
            return Err(ApiError::bad("阅读位置超出章节范围"));
        }
    } else if v.section != 0 {
        return Err(ApiError::bad("图片页不支持小说章节位置"));
    }
    let t = now();
    let sql = format!(
        "INSERT INTO reading_progress(item_id,page,section,offset,updated_at) VALUES(?,?,?,?,?) {}",
        state.db.upsert("page,section,offset,updated_at", "item_id")
    );
    state
        .db
        .execute(
            &sql,
            &[
                json!(id),
                json!(v.page),
                json!(v.section),
                json!(v.offset),
                json!(t),
            ],
        )
        .await?;
    Ok(Json(
        json!({"page":v.page,"section":v.section,"offset":v.offset,"updated_at":iso(&json!(t))}),
    ))
}
