use crate::{
    AppState,
    auth::DeviceId,
    error::{ApiError, Result},
    library::{Params, number},
};
use axum::{
    Json,
    body::{Body, Bytes},
    extract::{Query, Request, State},
    http::{StatusCode, header},
    response::{IntoResponse, Response},
};
use futures_util::StreamExt;
use rand::RngCore;
use serde_json::json;
use std::{
    collections::HashMap,
    sync::{Mutex, OnceLock},
    time::{Duration, Instant},
};
static ACTIVE: OnceLock<Mutex<HashMap<String, (String, Instant)>>> = OnceLock::new();
struct Slot {
    identity: String,
    token: String,
}
impl Drop for Slot {
    fn drop(&mut self) {
        if let Some(active) = ACTIVE.get()
            && let Ok(mut active) = active.lock()
            && active
                .get(&self.identity)
                .is_some_and(|v| v.0 == self.token)
        {
            active.remove(&self.identity);
        }
    }
}
fn slot(request: &Request) -> Result<Slot> {
    let identity = request
        .extensions()
        .get::<DeviceId>()
        .map(|v| v.0.clone())
        .unwrap_or_else(|| "administrator".into());
    let mut active = ACTIVE
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| ApiError::bad("测速服务不可用"))?;
    active.retain(|_, (_, t)| t.elapsed() < Duration::from_secs(30));
    if active.len() >= 4 || active.contains_key(&identity) {
        return Err(ApiError(
            StatusCode::TOO_MANY_REQUESTS,
            "服务器测速正在进行，请稍后重试",
        ));
    }
    let token = uuid::Uuid::new_v4().to_string();
    active.insert(identity.clone(), (token.clone(), Instant::now()));
    Ok(Slot { identity, token })
}
fn headers() -> [(&'static str, &'static str); 2] {
    [
        ("cache-control", "no-store, no-transform"),
        ("pragma", "no-cache"),
    ]
}
pub async fn ping() -> Response {
    (headers(),Json(json!({"ok":true,"max_bytes":32*1024*1024,"default_bytes":8*1024*1024,"timeout_seconds":30}))).into_response()
}
pub async fn download(Query(q): Query<Params>, request: Request) -> Result<Response> {
    let size = number(&q, "bytes", 8 * 1024 * 1024, 1, 32 * 1024 * 1024)? as usize;
    let slot = slot(&request)?;
    let stream = futures_util::stream::unfold(
        (size, slot, Instant::now()),
        |(left, slot, start)| async move {
            if left == 0 || start.elapsed() > Duration::from_secs(30) {
                return None;
            }
            let n = left.min(64 * 1024);
            let mut chunk = vec![0; n];
            rand::thread_rng().fill_bytes(&mut chunk);
            Some((
                Ok::<_, std::io::Error>(Bytes::from(chunk)),
                (left - n, slot, start),
            ))
        },
    );
    let mut response = (headers(), Body::from_stream(stream)).into_response();
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        "application/octet-stream".parse().unwrap(),
    );
    response
        .headers_mut()
        .insert(header::CONTENT_LENGTH, size.to_string().parse().unwrap());
    response
        .headers_mut()
        .insert(header::CONTENT_ENCODING, "identity".parse().unwrap());
    response
        .headers_mut()
        .insert("x-accel-buffering", "no".parse().unwrap());
    Ok(response)
}
pub async fn upload(State(_s): State<AppState>, request: Request) -> Result<Response> {
    let h = request.headers();
    if h.get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        != "application/octet-stream"
        || h.get(header::CONTENT_ENCODING)
            .is_some_and(|v| v != "identity")
    {
        return Err(ApiError(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "测速上传须使用未压缩的 application/octet-stream",
        ));
    }
    let declared = h
        .get(header::CONTENT_LENGTH)
        .map(|v| {
            v.to_str()
                .ok()
                .and_then(|s| s.parse::<usize>().ok())
                .ok_or_else(|| ApiError::bad("测速数据长度无效"))
        })
        .transpose()?;
    if declared.is_some_and(|n| n > 32 * 1024 * 1024) {
        return Err(ApiError(
            StatusCode::PAYLOAD_TOO_LARGE,
            "测速数据不能超过 32 MiB",
        ));
    }
    let _slot = slot(&request)?;
    let start = Instant::now();
    let mut stream = request.into_body().into_data_stream();
    let received = tokio::time::timeout(Duration::from_secs(30), async move {
        let mut received = 0;
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| ApiError::bad("测速上传已中断"))?;
            received += chunk.len();
            if received > 32 * 1024 * 1024 {
                return Err(ApiError(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "测速数据不能超过 32 MiB",
                ));
            }
        }
        Ok(received)
    })
    .await
    .map_err(|_| ApiError(StatusCode::REQUEST_TIMEOUT, "测速上传超时"))??;
    if declared.is_some_and(|n| n != received) {
        return Err(ApiError::bad("测速上传数据不完整"));
    }
    Ok((headers(),Json(json!({"received_bytes":received,"elapsed_ms":(start.elapsed().as_secs_f64()*1_000_000.0).round()/1000.0}))).into_response())
}
