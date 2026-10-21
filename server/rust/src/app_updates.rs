use crate::{
    AppState, content_store,
    db::{iso, now},
    error::{ApiError, Result},
    media, queue,
};
use axum::{
    Json,
    body::Body,
    extract::{Path, Query, Request, State},
    http::{StatusCode, header},
    response::Response,
};
use futures_util::StreamExt;
use serde_json::{Value, json};
use std::time::Duration;
use tokio::io::AsyncWriteExt;
pub const PACKAGE: &str = "app.qiye.qiye_library";
const CERT: &str = "5a93e25be4ef0f0e68033ab47cc8a767e8844c518a2714eaf5b3d97c976188e2";
fn view(row: &Value) -> Value {
    json!({"package_name":PACKAGE,"version_code":row["version_code"],"version_name":row["version_name"],"sha256":row["sha256"],"size_bytes":row["size_bytes"],"min_sdk":row["min_sdk"],"certificate_sha256":row["certificate_sha256"],"notes":row["notes"],"published_at":iso(&row["created_at"]),"download_path":format!("/api/app-updates/android/{}/apk",row["sha256"].as_str().unwrap_or(""))})
}
pub async fn latest(State(s): State<AppState>) -> Result<Json<Value>> {
    let release =
        s.db.one(
            "SELECT * FROM android_releases ORDER BY version_code DESC LIMIT 1",
            &[],
        )
        .await?;
    Ok(Json(json!({"release":release.as_ref().map(view)})))
}
async fn tool(program: &str, args: &[&str], path: &std::path::Path) -> Result<String> {
    let output = tempfile::tempfile()?;
    let stdout = output.try_clone()?;
    let mut command = tokio::process::Command::new(program);
    command
        .args(args)
        .arg(path)
        .stdin(std::process::Stdio::null())
        .stdout(stdout)
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true);
    #[cfg(windows)]
    command.creation_flags(0x08000000);
    let status = tokio::time::timeout(Duration::from_secs(90), command.status())
        .await
        .map_err(|_| ApiError::bad("APK 校验超时"))?
        .map_err(|_| {
            ApiError(
                StatusCode::SERVICE_UNAVAILABLE,
                "服务器需要安装 apksigner、aapt 和 Java",
            )
        })?;
    if !status.success() {
        return Err(ApiError::bad("APK 无效或签名校验失败"));
    }
    tokio::task::spawn_blocking(move || {
        use std::io::{Read, Seek, SeekFrom};
        let mut output = output;
        output.seek(SeekFrom::Start(0))?;
        let mut bytes = vec![];
        output.take(128 * 1024 + 1).read_to_end(&mut bytes)?;
        if bytes.len() > 128 * 1024 {
            return Err(ApiError::bad("APK 校验输出超过限制"));
        }
        Ok(String::from_utf8_lossy(&bytes).replace("\r\n", "\n"))
    })
    .await
    .map_err(|_| ApiError::bad("APK 校验失败"))?
}
pub fn parse_metadata(signature: &str, metadata: &str, certificate: &str) -> Result<Value> {
    let certs =
        regex::Regex::new(r"(?m)^Signer #\d+ certificate SHA-256 digest: ([a-fA-F0-9]{64})$")
            .unwrap()
            .captures_iter(signature)
            .map(|c| c[1].to_ascii_lowercase())
            .collect::<Vec<_>>();
    if certs != vec![certificate.to_owned()]
        || !regex::Regex::new(r"^[a-f0-9]{64}$")
            .unwrap()
            .is_match(certificate)
        || !signature.contains("Verified using v2 scheme (APK Signature Scheme v2): true")
    {
        return Err(ApiError::bad(
            "APK 签名与发布证书不一致，或缺少有效 v2 签名",
        ));
    }
    let re = regex::Regex::new(
        r"(?m)^package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'",
    )
    .unwrap();
    let p = re
        .captures(metadata)
        .ok_or_else(|| ApiError::bad("APK 版本信息无效"))?;
    let sdk = regex::Regex::new(r"(?m)^sdkVersion:'(\d+)'")
        .unwrap()
        .captures(metadata)
        .ok_or_else(|| ApiError::bad("APK SDK 信息无效"))?;
    let code = p[2]
        .parse::<i64>()
        .ok()
        .filter(|n| (1..=2100000000).contains(n))
        .ok_or_else(|| ApiError::bad("APK 版本号无效"))?;
    if &p[1] != PACKAGE
        || metadata.contains("application-debuggable")
        || !regex::Regex::new(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,79}$")
            .unwrap()
            .is_match(&p[3])
    {
        return Err(ApiError::bad("APK 包名、版本或正式发布配置无效"));
    }
    Ok(
        json!({"version_code":code,"version_name":&p[3],"min_sdk":sdk[1].parse::<u32>().map_err(|_|ApiError::bad("APK SDK 信息无效"))?,"certificate_sha256":certificate}),
    )
}
pub async fn upload(
    State(s): State<AppState>,
    Query(q): Query<crate::library::Params>,
    request: Request,
) -> Result<Json<Value>> {
    s.config.writable()?;
    if request
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        != "application/vnd.android.package-archive"
    {
        return Err(ApiError(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "请选择 APK 安装包",
        ));
    }
    let notes = crate::library::text(&q, "notes");
    if notes.chars().count() > 2000 {
        return Err(ApiError::invalid("更新说明不能超过 2000 字"));
    }
    let length = request
        .headers()
        .get(header::CONTENT_LENGTH)
        .map(|v| {
            v.to_str()
                .ok()
                .and_then(|s| s.parse::<u64>().ok())
                .ok_or_else(|| ApiError::bad("安装包大小无效"))
        })
        .transpose()?;
    if length.is_some_and(|n| n > 256 * 1024 * 1024) {
        return Err(ApiError(
            StatusCode::PAYLOAD_TOO_LARGE,
            "APK 不能超过 256 MiB",
        ));
    }
    let _permit = s
        .apk_limit
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError(StatusCode::CONFLICT, "另一安装包正在发布"))?;
    if !media::storage_ok(&s).await {
        return Err(ApiError::bad("书库存储不可用，请检查 NAS 挂载"));
    }
    let file = tempfile::Builder::new().suffix(".apk").tempfile()?;
    let mut output = tokio::fs::File::from_std(file.reopen()?);
    let mut body = request.into_body().into_data_stream();
    let received = tokio::time::timeout(Duration::from_secs(300), async {
        let mut size = 0;
        while let Some(chunk) = body.next().await {
            let chunk = chunk.map_err(|_| ApiError::bad("APK 上传中断"))?;
            size += chunk.len() as u64;
            if size > 256 * 1024 * 1024 {
                return Err(ApiError(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "APK 不能超过 256 MiB",
                ));
            }
            output.write_all(&chunk).await?;
        }
        output.flush().await?;
        Ok(size)
    })
    .await
    .map_err(|_| ApiError(StatusCode::REQUEST_TIMEOUT, "APK 上传超时"))??;
    drop(output);
    if received == 0 || length.is_some_and(|n| n != 0 && n != received) {
        return Err(ApiError::bad("APK 为空或上传不完整"));
    }
    let signer = std::env::var("LIBRARY_APKSIGNER").unwrap_or_else(|_| "apksigner".into());
    let aapt = std::env::var("LIBRARY_AAPT").unwrap_or_else(|_| "aapt".into());
    let signature = tool(
        &signer,
        &["verify", "--verbose", "--print-certs"],
        file.path(),
    )
    .await?;
    let metadata = tool(&aapt, &["dump", "badging"], file.path()).await?;
    let cert = std::env::var("LIBRARY_ANDROID_CERT_SHA256")
        .unwrap_or_else(|_| CERT.into())
        .to_ascii_lowercase();
    let info = parse_metadata(&signature, &metadata, &cert)?;
    let (relative, digest, size) =
        content_store::save_file(&s, "local", file.path(), 256 * 1024 * 1024).await?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    if let Some(previous) = tx
        .query(
            "SELECT * FROM android_releases WHERE version_code=?",
            &[info["version_code"].clone()],
        )
        .await?
        .pop()
    {
        if previous["sha256"] != digest {
            return Err(ApiError(
                StatusCode::CONFLICT,
                "此版本号已发布其他安装包，请提高 versionCode",
            ));
        }
        tx.commit().await?;
        return Ok(Json(json!({"release":view(&previous)})));
    }
    if tx
        .query(
            "SELECT version_code FROM android_releases ORDER BY version_code DESC LIMIT 1",
            &[],
        )
        .await?
        .pop()
        .is_some_and(|v| {
            v["version_code"].as_i64().unwrap_or(0) >= info["version_code"].as_i64().unwrap()
        })
    {
        return Err(ApiError(StatusCode::CONFLICT, "只能发布更高版本号的安装包"));
    }
    tx.execute("INSERT INTO android_releases(version_code,version_name,sha256,certificate_sha256,size_bytes,min_sdk,relative_path,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?)",&[info["version_code"].clone(),info["version_name"].clone(),json!(digest),json!(cert),json!(size),info["min_sdk"].clone(),json!(relative),json!(notes.trim()),json!(now())]).await?;
    let row = tx
        .query(
            "SELECT * FROM android_releases WHERE version_code=?",
            &[info["version_code"].clone()],
        )
        .await?
        .remove(0);
    tx.commit().await?;
    Ok(Json(json!({"release":view(&row)})))
}
pub async fn download(
    State(s): State<AppState>,
    Path(digest): Path<String>,
    request: Request<Body>,
) -> Result<Response> {
    if !regex::Regex::new(r"^[a-f0-9]{64}$")
        .unwrap()
        .is_match(&digest)
    {
        return Err(ApiError::missing("安装包不存在"));
    }
    let row =
        s.db.one(
            "SELECT * FROM android_releases WHERE sha256=?",
            &[json!(digest)],
        )
        .await?
        .ok_or_else(|| ApiError::missing("安装包不存在"))?;
    let root = s.config.storage_root.clone();
    let relative = row["relative_path"].as_str().unwrap_or("").to_owned();
    let size = row["size_bytes"].as_u64().unwrap_or(0);
    let path = media::io(&s, move || {
        let path = media::resolve(&root, &relative)?;
        content_store::verify_file(&path, &digest, size)?;
        Ok(path)
    })
    .await?;
    let mut response =
        media::serve(path, "application/vnd.android.package-archive", request).await?;
    response.headers_mut().insert(
        header::CONTENT_DISPOSITION,
        format!(
            "attachment; filename=\"qiye-android-{}.apk\"",
            row["version_code"]
        )
        .parse()
        .unwrap(),
    );
    Ok(response)
}
