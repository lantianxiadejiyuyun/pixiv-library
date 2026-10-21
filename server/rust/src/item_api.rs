use crate::{
    ApiJson, AppState,
    content_store::{self, Staging, Work},
    db::now,
    error::{ApiError, Result},
    library, media, queue, tasks,
};
use axum::{
    Json,
    body::{Body, Bytes},
    extract::{Multipart, Path, State},
    response::{IntoResponse, Response},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    path::PathBuf,
};
fn task_error(e: tasks::TaskError) -> ApiError {
    match e {
        tasks::TaskError::Failed(e) => e,
        _ => ApiError::bad("导入已中断"),
    }
}
#[derive(Deserialize)]
pub struct Changes {
    title: Option<String>,
    description: Option<String>,
    author: Option<String>,
    language: Option<String>,
    rating: Option<String>,
    tags: Option<Vec<String>>,
}
pub async fn patch(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(v): ApiJson<Changes>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    for (text, max) in [
        (&v.title, 512),
        (&v.description, 50000),
        (&v.author, 512),
        (&v.language, 32),
    ] {
        if text.as_ref().is_some_and(|v| v.chars().count() > max) {
            return Err(ApiError::invalid("作品字段超过长度限制"));
        }
    }
    if v.title.as_ref().is_some_and(|t| t.trim().is_empty()) {
        return Err(ApiError::bad("标题不能为空"));
    }
    if v.rating
        .as_ref()
        .is_some_and(|r| !["unknown", "general", "sensitive", "adult"].contains(&r.as_str()))
    {
        return Err(ApiError::invalid("分级无效"));
    }
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let old = tx
        .query("SELECT * FROM items WHERE id=?", &[json!(id)])
        .await?
        .pop()
        .ok_or_else(|| ApiError::missing("作品不存在"))?;
    let mut fields = vec![];
    let mut args = vec![];
    for (key, value) in [
        ("title", v.title),
        ("description", v.description),
        ("author", v.author),
        ("language", v.language),
        ("rating", v.rating),
    ] {
        if let Some(value) = value {
            if key == "title" && old["title"] != value {
                fields.push("title_zh=NULL".into());
                fields.push("title_zh_source=NULL".into());
            }
            if key == "rating" {
                fields.push("rating_origin='manual'".into());
            }
            fields.push(format!("{key}=?"));
            args.push(json!(value));
        }
    }
    if let Some(tags) = v.tags {
        content_store::set_tags(&s, &mut tx, &id, &tags, "manual").await?;
    }
    fields.push("updated_at=?".into());
    args.extend([json!(now()), json!(id)]);
    tx.execute(
        &format!("UPDATE items SET {} WHERE id=?", fields.join(",")),
        &args,
    )
    .await?;
    tx.commit().await?;
    library::detail(State(s), Path(id)).await
}
pub async fn remove(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    if tx
        .query("SELECT id FROM items WHERE id=?", &[json!(id)])
        .await?
        .is_empty()
    {
        return Err(ApiError::missing("作品不存在"));
    }
    tx.execute("UPDATE series SET revision=revision+1,complete=CASE WHEN origin='source' THEN 0 ELSE complete END,updated_at=? WHERE id IN (SELECT series_id FROM series_entries WHERE item_id=?)",&[json!(now()),json!(id)]).await?;
    tx.execute("DELETE FROM items WHERE id=?", &[json!(id)])
        .await?;
    tx.commit().await?;
    Ok(Json(json!({"ok":true})))
}
pub async fn cover_refresh(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let item = library::require_item(&s.db, &id).await?;
    if item["source"] != "pixiv" || item["source_id"].is_null() {
        return Err(ApiError::bad("此作品暂不支持补全远端封面"));
    }
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let j = tasks::enqueue_import_tx(
        &s,
        &mut tx,
        json!({"platform":"pixiv","remote_id":item["source_id"],"item_id":id,"download":false}),
    )
    .await?;
    tx.commit().await?;
    Ok(Json(
        json!({"job_id":j["id"],"message":"已加入封面队列，正文保持不变"}),
    ))
}
pub async fn covers(State(s): State<AppState>, ApiJson(v): ApiJson<Value>) -> Result<Json<Value>> {
    s.config.writable()?;
    let source = v["source"].as_str();
    if let Some(source) = source {
        content_store::source(source)?;
        if source == "local" {
            return Err(ApiError::bad("本地作品没有远端封面"));
        }
    }
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let mut args = vec![];
    let filter = if let Some(source) = source {
        args.push(json!(source));
        "source=?"
    } else {
        "source IN ('pixiv','picacg','ehentai','jm','kemono')"
    };
    let rows=tx.query(&format!("SELECT id,source,source_id FROM items WHERE {filter} AND source_id IS NOT NULL AND preview_cover IS NULL AND type IN ('novel','article') ORDER BY created_at,id LIMIT 100"),&args).await?;
    let mut jobs = vec![];
    for row in rows {
        jobs.push(tasks::enqueue_import_tx(&s,&mut tx,json!({"platform":row["source"],"remote_id":row["source_id"],"item_id":row["id"],"download":false})).await?["id"].clone());
    }
    tx.commit().await?;
    Ok(Json(
        json!({"queued":jobs.len(),"job_ids":jobs,"message":"缺失封面已加入详情队列"}),
    ))
}
async fn import(
    s: &AppState,
    title: Option<String>,
    kind: String,
    staging: Staging,
) -> Result<String> {
    if !["manga", "illustration", "novel", "article"].contains(&kind.as_str()) {
        return Err(ApiError::invalid("内容类型无效"));
    }
    let staging = staging.expand(s).await.map_err(task_error)?;
    let title = title.filter(|s| !s.trim().is_empty()).unwrap_or_else(|| {
        std::path::Path::new(&staging.entries[0].name)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("本地导入")
            .to_owned()
    });
    if title.chars().count() > 512 {
        return Err(ApiError::invalid("作品标题过长"));
    }
    let prepared = content_store::prepare(s, "local", staging, None, None)
        .await
        .map_err(task_error)?;
    let work = Work::new(&uuid::Uuid::new_v4().to_string(), title.trim(), &kind)?;
    let mut tx = s.db.begin().await?;
    queue::locked(s, &mut tx).await?;
    let item = content_store::merge(s, &mut tx, "local", &work).await?;
    let id = item["id"].as_str().unwrap();
    tx.execute("UPDATE items SET source_id=NULL WHERE id=?", &[json!(id)])
        .await?;
    content_store::attach(&mut tx, id, &prepared.assets, false).await?;
    tx.commit().await?;
    Ok(id.into())
}
pub async fn upload(State(s): State<AppState>, mut multipart: Multipart) -> Result<Json<Value>> {
    s.config.writable()?;
    let _permit = s.import_limit.clone().try_acquire_owned().map_err(|_| {
        ApiError(
            axum::http::StatusCode::CONFLICT,
            "另一项本地导入正在进行，请稍后重试",
        )
    })?;
    let mut staging = Staging::new()?;
    let mut title = None;
    let mut kind = "manga".to_owned();
    let mut fields = 0;
    while let Some(mut field) = multipart
        .next_field()
        .await
        .map_err(|_| ApiError::bad("上传表单不完整或超过限制"))?
    {
        fields += 1;
        if fields > content_store::MAX_FILES + 2 {
            return Err(ApiError::bad("上传文件数量过多"));
        }
        let name = field.name().unwrap_or("").to_owned();
        if name == "files" {
            let filename = field.file_name().unwrap_or("upload").to_owned();
            staging
                .stream(&filename, field, None)
                .await
                .map_err(task_error)?;
        } else if name == "title" || name == "type" {
            let mut bytes = vec![];
            while let Some(chunk) = field
                .chunk()
                .await
                .map_err(|_| ApiError::bad("上传表单无效"))?
            {
                if bytes.len() + chunk.len() > 2048 {
                    return Err(ApiError::bad("上传表单字段过长"));
                }
                bytes.extend_from_slice(&chunk);
            }
            let value = String::from_utf8(bytes.to_vec())
                .map_err(|_| ApiError::bad("上传表单不是 UTF-8"))?;
            if name == "title" {
                title = Some(value);
            } else {
                kind = value;
            }
        }
    }
    let id = import(&s, title, kind, staging).await?;
    library::detail(State(s), Path(id)).await
}
#[derive(Deserialize)]
pub struct Folder {
    path: String,
    title: Option<String>,
    #[serde(default = "kind")]
    r#type: String,
}
fn kind() -> String {
    "manga".into()
}
pub async fn folder(State(s): State<AppState>, ApiJson(v): ApiJson<Folder>) -> Result<Json<Value>> {
    s.config.writable()?;
    let _permit = s.import_limit.clone().try_acquire_owned().map_err(|_| {
        ApiError(
            axum::http::StatusCode::CONFLICT,
            "另一项本地导入正在进行，请稍后重试",
        )
    })?;
    if v.path.is_empty() || v.path.chars().count() > 2048 {
        return Err(ApiError::invalid("导入目录无效"));
    }
    let root = s.config.import_root.clone();
    let input = v.path;
    let (files, name) = media::io(&s, move || {
        let root = root.canonicalize()?;
        let input = PathBuf::from(input);
        let path = if input.is_absolute() {
            input
        } else {
            root.join(input)
        }
        .canonicalize()?;
        if !path.starts_with(&root) || !path.is_dir() {
            return Err(ApiError::bad("只能导入 LIBRARY_IMPORT_ROOT 内的文件夹"));
        }
        let name = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("本地导入")
            .to_owned();
        let mut stack = vec![(path, 0)];
        let mut files = vec![];
        let mut inspected = 0;
        while let Some((dir, depth)) = stack.pop() {
            if depth > 64 {
                return Err(ApiError::bad("导入目录层级过深"));
            }
            for entry in std::fs::read_dir(dir)? {
                let entry = entry?;
                let p = entry.path();
                let metadata = std::fs::symlink_metadata(&p)?;
                inspected += 1;
                if metadata.file_type().is_symlink() || !p.canonicalize()?.starts_with(&root) {
                    return Err(ApiError::bad("导入目录不允许符号链接或外部路径"));
                }
                if inspected > 10000 {
                    return Err(ApiError::bad("导入目录条目过多"));
                }
                if metadata.is_dir() {
                    stack.push((p, depth + 1));
                } else if content_store::supported(p.to_str().unwrap_or(""))
                    || ["zip", "cbz"]
                        .contains(&content_store::extension(p.to_str().unwrap_or("")).as_str())
                {
                    if files.len() >= content_store::MAX_FILES {
                        return Err(ApiError::bad("导入文件过多"));
                    }
                    files.push(p);
                }
            }
        }
        files.sort_by(|a, b| content_store::natural(&a.to_string_lossy(), &b.to_string_lossy()));
        Ok((files, name))
    })
    .await?;
    let mut staging = Staging::new()?;
    for file in files {
        let name = file
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("upload")
            .to_owned();
        let bytes = media::io(&s, move || {
            let mut bytes = vec![];
            std::fs::File::open(file)?
                .take(content_store::MAX_FILE + 1)
                .read_to_end(&mut bytes)?;
            if bytes.len() as u64 > content_store::MAX_FILE {
                return Err(ApiError::bad("导入文件超过 128 MiB"));
            }
            Ok(bytes)
        })
        .await?;
        staging
            .bytes(&name, &bytes, None)
            .await
            .map_err(task_error)?;
    }
    let id = import(&s, v.title.or(Some(name)), v.r#type, staging).await?;
    library::detail(State(s), Path(id)).await
}
pub async fn temporary(
    file: tempfile::NamedTempFile,
    filename: &str,
    mime: &str,
) -> Result<Response> {
    temporary_owned(file, filename, mime, None).await
}
pub async fn temporary_owned(
    file: tempfile::NamedTempFile,
    filename: &str,
    mime: &str,
    guard: Option<tokio::sync::OwnedSemaphorePermit>,
) -> Result<Response> {
    use tokio::io::AsyncReadExt;
    let size = file.as_file().metadata()?.len();
    let reader = tokio::fs::File::from_std(file.reopen()?);
    let stream = futures_util::stream::try_unfold(
        (reader, file, guard),
        |(mut reader, file, guard)| async move {
            let mut chunk = vec![0; 64 * 1024];
            let n = reader.read(&mut chunk).await?;
            if n == 0 {
                Ok::<_, std::io::Error>(None)
            } else {
                chunk.truncate(n);
                Ok(Some((Bytes::from(chunk), (reader, file, guard))))
            }
        },
    );
    let encoded = url::form_urlencoded::byte_serialize(filename.as_bytes())
        .collect::<String>()
        .replace('+', "%20");
    Ok((
        [
            ("content-type", mime.to_owned()),
            ("content-length", size.to_string()),
            (
                "content-disposition",
                format!("attachment; filename*=UTF-8''{encoded}"),
            ),
        ],
        Body::from_stream(stream),
    )
        .into_response())
}
pub async fn export(State(s): State<AppState>, Path(id): Path<String>) -> Result<Response> {
    let export_permit = s.export_limit.clone().try_acquire_owned().map_err(|_| {
        ApiError(
            axum::http::StatusCode::CONFLICT,
            "已有作品导出正在进行，请稍后重试",
        )
    })?;
    let item = library::require_item(&s.db, &id).await?;
    let assets =
        s.db.query(
            "SELECT * FROM assets WHERE item_id=? ORDER BY position",
            &[json!(id)],
        )
        .await?;
    if assets.is_empty() {
        return Err(ApiError(
            axum::http::StatusCode::CONFLICT,
            "作品仅含元数据，请先下载原文件",
        ));
    }
    if assets.len() > 3000
        || assets
            .iter()
            .map(|a| a["size_bytes"].as_u64().unwrap_or(u64::MAX))
            .try_fold(0u64, |sum, n| sum.checked_add(n))
            .is_none_or(|n| n > 2 * 1024 * 1024 * 1024)
    {
        return Err(ApiError::bad("导出作品超过 3000 个文件或 2 GiB 限制"));
    }
    let extension = if assets.iter().all(|a| {
        a["mime_type"]
            .as_str()
            .is_some_and(|m| m.starts_with("image/"))
    }) {
        "cbz"
    } else {
        "zip"
    };
    let root = s.config.storage_root.clone();
    let permit = s
        .fs_limit
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| ApiError::bad("文件服务不可用"))?;
    let file = tokio::task::spawn_blocking(move || -> Result<_> {
        let _permit = permit;
        let mut output = tempfile::NamedTempFile::new()?;
        {
            let mut zip = zip::ZipWriter::new(output.as_file_mut());
            for a in assets {
                let path = media::resolve(&root, a["relative_path"].as_str().unwrap_or(""))?;
                let size = a["size_bytes"].as_u64().unwrap_or(0);
                if !path.is_file() || path.metadata()?.len() != size {
                    return Err(ApiError::bad("原文件缺失，请检查 NAS 后重试"));
                }
                let name =
                    content_store::clean_name(a["original_name"].as_str().unwrap_or("file"))?;
                zip.start_file(
                    format!("{:04}_{name}", a["position"].as_i64().unwrap_or(0) + 1),
                    zip::write::FileOptions::default()
                        .compression_method(zip::CompressionMethod::Stored),
                )
                .map_err(|_| ApiError::bad("导出文件写入失败"))?;
                std::io::copy(&mut std::fs::File::open(path)?, &mut zip)?;
            }
            zip.finish()
                .map_err(|_| ApiError::bad("导出文件写入失败"))?;
        }
        output.flush()?;
        Ok(output)
    })
    .await
    .map_err(|_| ApiError::bad("导出处理失败"))??;
    temporary_owned(
        file,
        &format!(
            "{}.{extension}",
            item["title"]
                .as_str()
                .unwrap_or("library")
                .chars()
                .take(100)
                .collect::<String>()
        ),
        "application/zip",
        Some(export_permit),
    )
    .await
}
