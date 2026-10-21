use crate::{
    ApiJson, AppState,
    config::Config,
    db::now,
    error::{ApiError, Result},
};
use axum::{
    Json,
    extract::{ConnectInfo, Extension, Request, State},
    http::{HeaderMap, StatusCode, header},
    middleware::Next,
    response::{IntoResponse, Response},
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use hmac::{Hmac, Mac};
use rand::RngCore;
use regex::Regex;
use serde::Deserialize;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, VecDeque},
    net::SocketAddr,
    sync::{Mutex, OnceLock},
    time::{Duration, Instant},
};
use subtle::ConstantTimeEq;

pub const SESSION_SECONDS: u64 = 604800;
pub const COOKIE: &str = "qiye_session";
#[derive(Default)]
pub struct Rates(Mutex<HashMap<String, Bucket>>);
struct Bucket {
    events: VecDeque<Instant>,
    window: Duration,
}
fn prune(buckets: &mut HashMap<String, Bucket>, t: Instant) {
    buckets.retain(|_, q| {
        while q
            .events
            .front()
            .is_some_and(|v| t.duration_since(*v) >= q.window)
        {
            q.events.pop_front();
        }
        !q.events.is_empty()
    });
}
impl Rates {
    pub fn take(&self, key: String, limit: usize, window: Duration) -> Result<()> {
        let mut buckets = self.0.lock().unwrap_or_else(|e| e.into_inner());
        let t = Instant::now();
        prune(&mut buckets, t);
        if buckets.len() >= 1024 && !buckets.contains_key(&key) {
            return Err(ApiError(
                StatusCode::TOO_MANY_REQUESTS,
                "请求过于频繁，请稍后重试",
            ));
        }
        let q = &mut buckets
            .entry(key)
            .or_insert_with(|| Bucket {
                events: VecDeque::new(),
                window,
            })
            .events;
        if q.len() >= limit {
            return Err(ApiError(
                StatusCode::TOO_MANY_REQUESTS,
                "请求过于频繁，请稍后重试",
            ));
        }
        q.push_back(t);
        Ok(())
    }
    pub fn password(&self, key: String, password: &str, expected: &str) -> Result<()> {
        let valid = bool::from(
            Sha256::digest(password.as_bytes()).ct_eq(&Sha256::digest(expected.as_bytes())),
        );
        let t = Instant::now();
        let mut buckets = self.0.lock().unwrap_or_else(|e| e.into_inner());
        prune(&mut buckets, t);
        if buckets.get(&key).is_some_and(|q| q.events.len() >= 12)
            || buckets.len() >= 1024 && !buckets.contains_key(&key)
        {
            return Err(ApiError(
                StatusCode::TOO_MANY_REQUESTS,
                "登录尝试过多，请五分钟后重试",
            ));
        }
        if valid {
            buckets.remove(&key);
            return Ok(());
        }
        buckets
            .entry(key)
            .or_insert_with(|| Bucket {
                events: VecDeque::new(),
                window: Duration::from_secs(300),
            })
            .events
            .push_back(t);
        Err(ApiError(StatusCode::UNAUTHORIZED, "密码错误"))
    }
}
pub fn random_token(prefix: &str, bytes: usize) -> String {
    let mut b = vec![0; bytes];
    rand::thread_rng().fill_bytes(&mut b);
    format!("{prefix}{}", URL_SAFE_NO_PAD.encode(b))
}
pub fn digest(s: &str) -> String {
    format!("{:x}", Sha256::digest(s.as_bytes()))
}
pub fn issue(config: &Config) -> String {
    let payload=URL_SAFE_NO_PAD.encode(json!({"exp":chrono::Utc::now().timestamp()+SESSION_SECONDS as i64,"nonce":random_token("",16)}).to_string());
    let mut mac = Hmac::<Sha256>::new_from_slice(config.secret.as_bytes())
        .expect("HMAC accepts any key size");
    mac.update(payload.as_bytes());
    format!("{payload}.{:x}", mac.finalize().into_bytes())
}
pub fn authenticated(headers: &HeaderMap, config: &Config) -> bool {
    let Some(cookies) = headers.get(header::COOKIE).and_then(|h| h.to_str().ok()) else {
        return false;
    };
    let Some(token) = cookies
        .split(';')
        .map(str::trim)
        .find_map(|v| v.strip_prefix("qiye_session="))
    else {
        return false;
    };
    if token.len() > 1024 {
        return false;
    }
    let Some((payload, sig)) = token.rsplit_once('.') else {
        return false;
    };
    if sig.len() != 64 {
        return false;
    }
    let mut signature = Vec::with_capacity(32);
    for i in (0..64).step_by(2) {
        let Some(part) = sig.get(i..i + 2) else {
            return false;
        };
        let Ok(v) = u8::from_str_radix(part, 16) else {
            return false;
        };
        signature.push(v);
    }
    let mut mac = Hmac::<Sha256>::new_from_slice(config.secret.as_bytes()).expect("valid HMAC");
    mac.update(payload.as_bytes());
    if mac.verify_slice(&signature).is_err() {
        return false;
    }
    URL_SAFE_NO_PAD
        .decode(payload)
        .ok()
        .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok())
        .and_then(|v| v["exp"].as_i64())
        .is_some_and(|t| t > chrono::Utc::now().timestamp())
}
fn permitted(method: &str, path: &str) -> bool {
    static PATHS: OnceLock<Vec<(&str, Regex)>> = OnceLock::new();
    PATHS.get_or_init(||vec![
        ("GET",Regex::new(r"^/api/(?:health|dashboard|items|tags|favorite-groups|library/collections|platform-endpoints|translation/status|network/speed/(?:ping|download)|tag-tracking|characters(?:/[^/]+)?|app-updates/android(?:/[a-f0-9]{64}/apk)?|pixiv/watchlist(?:/[^/]+)?|pixiv/authors(?:/[^/]+(?:/(?:avatar|feed))?)?|pixiv/tasks/[^/]+|items/[^/]+|items/[^/]+/(?:cover|export|translation)|assets/[^/]+/(?:content|thumbnail|text-sections)|series/[^/]+)$").unwrap()),
        ("PUT",Regex::new(r"^/api/(?:items/[^/]+/(?:progress|favorite|score)|favorite-groups/[^/]+|pixiv/authors/[^/]+/download-settings|platform-endpoints/(?:pixiv|ehentai|picacg|jm|kemono))$").unwrap()),
        ("POST",Regex::new(r"^/api/(?:favorite-groups|items/[^/]+/(?:visit|download|cover-refresh|translate)|devices/disconnect|pixiv/authors/open|pixiv/authors/[^/]+/feed|pixiv/watchlist/sync|pixiv/watchlist/[^/]+/(?:read|check)|pixiv/direct/credentials|characters(?:/search|/[^/]+/profile)?|network/speed/upload|tag-tracking(?:/[^/]+/check)?)$").unwrap()),
        ("DELETE",Regex::new(r"^/api/(?:favorite-groups/[^/]+|history|tag-tracking/[^/]+)$").unwrap()),
        ("PATCH",Regex::new(r"^/api/tag-tracking/[^/]+$").unwrap()),
        ("HEAD",Regex::new(r"^/api/assets/[^/]+/content$").unwrap()),
    ]).iter().any(|(m,p)|*m==method&&p.is_match(path))
}
pub async fn device(
    state: &AppState,
    headers: &HeaderMap,
    method: &str,
    path: &str,
) -> Result<String> {
    let values: Vec<_> = headers.get_all(header::AUTHORIZATION).iter().collect();
    let invalid = || ApiError(StatusCode::UNAUTHORIZED, "设备凭据无效，请重新扫码绑定");
    if values.len() != 1 {
        return Err(invalid());
    }
    let token = values[0]
        .to_str()
        .ok()
        .and_then(|v| v.strip_prefix("Bearer "))
        .ok_or_else(invalid)?;
    if token.len() != 74
        || !token.starts_with("ql_device_")
        || !token[10..]
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err(invalid());
    }
    let row=state.db.one("SELECT id FROM device_sessions WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?",&[json!(digest(token)),json!(now())]).await?.ok_or_else(invalid)?;
    if !permitted(method, path) {
        return Err(ApiError(
            StatusCode::FORBIDDEN,
            "此设备未获管理权限，请使用管理员登录",
        ));
    }
    let cutoff = (chrono::Utc::now() - chrono::Duration::seconds(60))
        .naive_utc()
        .to_string();
    if !state.config.read_only {
        state.db.execute("UPDATE device_sessions SET last_used_at=? WHERE id=? AND revoked_at IS NULL AND expires_at>? AND (last_used_at IS NULL OR last_used_at<?)",&[json!(now()),row["id"].clone(),json!(now()),json!(cutoff)]).await?;
    }
    Ok(row["id"].as_str().unwrap_or_default().to_owned())
}
pub async fn guard(State(state): State<AppState>, mut req: Request, next: Next) -> Response {
    let path = req.uri().path().to_owned();
    let origin = req
        .headers()
        .get(header::ORIGIN)
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned);
    let wildcard = state.config.allowed_origins.iter().any(|s| s == "*");
    let explicit = origin.as_ref().is_some_and(|o| {
        state
            .config
            .allowed_origins
            .contains(&o.trim_end_matches('/').to_owned())
    });
    let host = req
        .headers()
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default();
    let scheme = req
        .uri()
        .scheme_str()
        .unwrap_or(if state.config.secure_cookie {
            "https"
        } else {
            "http"
        });
    let same = origin
        .as_deref()
        .is_some_and(|o| o.trim_end_matches('/') == format!("{scheme}://{host}"));
    let cors = wildcard || explicit || same;
    let processing = async {
        let result: Result<Option<Response>> = async {
            if req.method() == axum::http::Method::OPTIONS {
                if origin.is_some() && !cors {
                    return Err(ApiError(StatusCode::FORBIDDEN, "请求来源未获允许"));
                }
                return Ok(Some(StatusCode::NO_CONTENT.into_response()));
            }
            if path.starts_with("/api/") {
                if !matches!(req.method().as_str(), "GET" | "HEAD")
                    && !wildcard
                    && (origin.is_some() && !cors
                        || req
                            .headers()
                            .get("sec-fetch-site")
                            .is_some_and(|v| v == "cross-site"))
                {
                    return Err(ApiError(StatusCode::FORBIDDEN, "不允许跨站写入请求"));
                }
                if req.headers().contains_key(header::AUTHORIZATION) {
                    let id = device(&state, req.headers(), req.method().as_str(), &path).await?;
                    req.extensions_mut().insert(DeviceId(id));
                } else if !matches!(
                    path.as_str(),
                    "/api/health"
                        | "/api/auth/status"
                        | "/api/auth/login"
                        | "/api/devices/pairing/exchange"
                ) && !authenticated(req.headers(), &state.config)
                {
                    return Err(ApiError(StatusCode::UNAUTHORIZED, "请先登录书库"));
                }
                // Never return the SPA for an API that has not been ported.
                if !crate::catalog::implemented(req.method().as_str(), &path) {
                    return Err(if crate::catalog::unported(req.method().as_str(), &path) {
                        ApiError(
                            StatusCode::NOT_IMPLEMENTED,
                            "此接口尚未迁移至 Rust，请继续使用原服务",
                        )
                    } else if crate::catalog::known_path(&path) {
                        ApiError(StatusCode::METHOD_NOT_ALLOWED, "此接口不支持该请求方法")
                    } else {
                        ApiError::missing("API 路由不存在")
                    });
                }
            }
            Ok(None)
        }
        .await;
        match result {
            Ok(Some(r)) => r,
            Ok(None) => next.run(req).await,
            Err(e) => e.into_response(),
        }
    };
    let timeout = if path.starts_with("/api/imports/")
        || path == "/api/database/export"
        || path.ends_with("/export")
    {
        600
    } else if path == "/api/app-updates/android" {
        400
    } else if path == "/api/translation/test" || path.ends_with("/models/unload") {
        190
    } else if path.starts_with("/api/sources/") && path.ends_with("/login")
        || path.ends_with("/oauth/finish")
    {
        120
    } else if path.contains("/ai/nodes/") && path.contains("/services/") {
        60
    } else if path.starts_with("/api/network/managed/") {
        120
    } else if path.starts_with("/api/ai/characters/")
        || path == "/api/characters/search"
        || path == "/api/characters"
        || path.ends_with("/profile")
    {
        45
    } else if path.starts_with("/api/network/speed/") {
        35
    } else {
        15
    };
    let mut res = tokio::time::timeout(Duration::from_secs(timeout), processing)
        .await
        .unwrap_or_else(|_| {
            ApiError(StatusCode::SERVICE_UNAVAILABLE, "请求处理超时，请稍后重试").into_response()
        });
    let h = res.headers_mut();
    h.insert("x-content-type-options", "nosniff".parse().unwrap());
    h.insert("x-frame-options", "DENY".parse().unwrap());
    h.insert("referrer-policy", "same-origin".parse().unwrap());
    if path.starts_with("/api/") {
        let no_transform = h
            .get(header::CACHE_CONTROL)
            .and_then(|v| v.to_str().ok())
            .is_some_and(|s| s.split(',').any(|s| s.trim() == "no-transform"));
        h.insert(
            header::CACHE_CONTROL,
            if no_transform {
                "no-store, no-transform"
            } else {
                "no-store"
            }
            .parse()
            .unwrap(),
        );
    }
    if cors && let Some(origin) = origin {
        if let Ok(v) = origin.parse() {
            h.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, v);
        }
        h.insert(
            header::ACCESS_CONTROL_ALLOW_CREDENTIALS,
            "true".parse().unwrap(),
        );
        h.insert(
            header::ACCESS_CONTROL_ALLOW_METHODS,
            "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"
                .parse()
                .unwrap(),
        );
        h.insert(
            header::ACCESS_CONTROL_ALLOW_HEADERS,
            "Authorization, Content-Type, Range".parse().unwrap(),
        );
        h.insert(
            header::ACCESS_CONTROL_EXPOSE_HEADERS,
            "Content-Length, Content-Range, Accept-Ranges, Content-Disposition"
                .parse()
                .unwrap(),
        );
        h.append(header::VARY, "Origin".parse().unwrap());
    }
    res
}
#[derive(Clone)]
pub struct DeviceId(pub String);
pub async fn status(State(state): State<AppState>, headers: HeaderMap) -> Json<serde_json::Value> {
    Json(json!({"authenticated":authenticated(&headers,&state.config)}))
}
#[derive(Deserialize)]
pub struct Login {
    password: String,
}
pub async fn login(
    State(state): State<AppState>,
    connect: Option<Extension<ConnectInfo<SocketAddr>>>,
    ApiJson(data): ApiJson<Login>,
) -> Result<Response> {
    if data.password.is_empty() || data.password.chars().count() > 1024 {
        return Err(ApiError::invalid("密码长度不符合要求"));
    }
    let identity = connect
        .map(|Extension(ConnectInfo(v))| v.ip().to_string())
        .unwrap_or_else(|| "test".to_owned());
    state.rates.password(
        format!("login:{identity}"),
        &data.password,
        &state.config.password,
    )?;
    let mut r = Json(json!({"ok":true})).into_response();
    let cookie = format!(
        "{COOKIE}={}; Max-Age={SESSION_SECONDS}; Path=/; HttpOnly; SameSite=Strict{}",
        issue(&state.config),
        if state.config.secure_cookie {
            "; Secure"
        } else {
            ""
        }
    );
    r.headers_mut()
        .insert(header::SET_COOKIE, cookie.parse().unwrap());
    Ok(r)
}
pub async fn logout(State(state): State<AppState>) -> Response {
    let mut r = Json(json!({"ok":true})).into_response();
    r.headers_mut().insert(
        header::SET_COOKIE,
        format!(
            "{COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict{}",
            if state.config.secure_cookie {
                "; Secure"
            } else {
                ""
            }
        )
        .parse()
        .unwrap(),
    );
    r
}
