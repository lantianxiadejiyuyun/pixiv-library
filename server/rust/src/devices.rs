use crate::{
    ApiJson, AppState,
    auth::{DeviceId, digest, random_token},
    db::{iso, now},
    error::{ApiError, Result},
};
use axum::{
    Json,
    extract::{ConnectInfo, Extension, Path, State},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    net::{IpAddr, SocketAddr},
    time::Duration,
};

#[derive(Deserialize)]
pub struct Pair {
    server_url: String,
}
pub fn normalize_url(raw: &str) -> Result<String> {
    let bad = || ApiError::invalid("服务器地址须为根地址；公网使用 HTTPS，局域网可使用 HTTP");
    let raw = raw.trim();
    if raw.chars().any(|c| c.is_control() || c.is_whitespace()) || raw.contains(['\\', '?', '#']) {
        return Err(bad());
    }
    let u = url::Url::parse(raw).map_err(|_| bad())?;
    let host = u.host_str().ok_or_else(bad)?;
    if !matches!(u.scheme(), "http" | "https")
        || !u.username().is_empty()
        || u.password().is_some()
        || u.path() != "/"
        || u.port() == Some(0)
    {
        return Err(bad());
    }
    let local = match host.trim_matches(['[', ']']).parse::<IpAddr>() {
        Ok(IpAddr::V4(a)) => {
            if a.is_unspecified() || a.is_multicast() {
                return Err(bad());
            }
            a.is_private() || a.is_loopback() || a.is_link_local()
        }
        Ok(IpAddr::V6(a)) => {
            if a.is_unspecified() || a.is_multicast() {
                return Err(bad());
            }
            a.is_loopback() || a.is_unique_local() || a.is_unicast_link_local()
        }
        Err(_) => {
            if host.len() > 253 || host.ends_with('.') || host.contains('%') {
                return Err(bad());
            }
            !host.contains('.')
                || host.ends_with(".local")
                || host.ends_with(".lan")
                || host.ends_with(".home.arpa")
        }
    };
    if u.scheme() == "http" && !local {
        return Err(bad());
    }
    Ok(u.as_str().trim_end_matches('/').to_owned())
}
pub async fn pair(State(s): State<AppState>, ApiJson(v): ApiJson<Pair>) -> Result<Json<Value>> {
    s.config.writable()?;
    let url = normalize_url(&v.server_url)?;
    let token = random_token("ql_pair_", 32);
    let id = uuid::Uuid::new_v4().to_string();
    let t = now();
    let expires = (chrono::Utc::now() + chrono::Duration::minutes(5))
        .naive_utc()
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string();
    let mut tx = s.db.begin().await?;
    tx.execute(
        "DELETE FROM device_pairings WHERE expires_at<=? OR consumed_at IS NOT NULL",
        &[json!(t)],
    )
    .await?;
    let sql = format!(
        "INSERT INTO device_pairings(id,slot,token_hash,created_at,expires_at,consumed_at) VALUES(?,1,?,?,?,NULL) {}",
        s.db.upsert("id,token_hash,created_at,expires_at,consumed_at", "slot")
    );
    tx.execute(
        &sql,
        &[json!(id), json!(digest(&token)), json!(t), json!(expires)],
    )
    .await?;
    tx.commit().await?;
    Ok(Json(
        json!({"id":id,"server_url":url,"expires_at":iso(&json!(expires)),"qr_payload":json!({"type":"qiye-library","version":1,"server_url":url,"pairing_token":token}).to_string()}),
    ))
}
#[derive(Deserialize)]
pub struct Exchange {
    pairing_token: String,
    device_name: String,
}
pub async fn exchange(
    State(s): State<AppState>,
    connect: Option<Extension<ConnectInfo<SocketAddr>>>,
    ApiJson(v): ApiJson<Exchange>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    s.rates
        .take("pairing:global".to_owned(), 60, Duration::from_secs(60))?;
    s.rates.take(
        format!(
            "pairing:{}",
            connect
                .map(|Extension(ConnectInfo(c))| c.ip().to_string())
                .unwrap_or_default()
        ),
        8,
        Duration::from_secs(60),
    )?;
    let bad = || ApiError::bad("绑定码无效或已过期，请在网页重新生成");
    if v.pairing_token.len() != 51
        || !v.pairing_token.starts_with("ql_pair_")
        || !v.pairing_token[8..]
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err(bad());
    }
    let name = v.device_name.trim();
    if name.is_empty() || name.chars().count() > 80 || name.chars().any(char::is_control) {
        return Err(ApiError::invalid("设备名称无效"));
    }
    let mut tx = s.db.begin().await?;
    let t = now();
    if tx.execute("UPDATE device_pairings SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL AND expires_at>?",&[json!(t),json!(digest(&v.pairing_token)),json!(t)]).await?!=1{return Err(bad());}
    let count = tx
        .query(
            "SELECT COUNT(*) AS n FROM device_sessions WHERE revoked_at IS NULL AND expires_at>?",
            &[json!(t)],
        )
        .await?;
    if count[0]["n"].as_i64().unwrap_or(0) >= 50 {
        return Err(ApiError(
            axum::http::StatusCode::CONFLICT,
            "已达到 50 台有效设备，请先撤销旧设备",
        ));
    }
    let id = uuid::Uuid::new_v4().to_string();
    let token = random_token("ql_device_", 48);
    let expires = (chrono::Utc::now() + chrono::Duration::days(90))
        .naive_utc()
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string();
    tx.execute("INSERT INTO device_sessions(id,token_hash,device_name,created_at,last_used_at,expires_at,revoked_at) VALUES(?,?,?,?,NULL,?,NULL)",&[json!(id),json!(digest(&token)),json!(name),json!(t),json!(expires)]).await?;
    tx.commit().await?;
    Ok(Json(
        json!({"device_id":id,"device_name":name,"access_token":token,"expires_at":iso(&json!(expires)),"server_name":"栖页"}),
    ))
}
pub async fn list(State(s): State<AppState>) -> Result<Json<Value>> {
    let mut rows=s.db.query("SELECT id,device_name,created_at,last_used_at,expires_at,revoked_at FROM device_sessions ORDER BY created_at DESC,id",&[]).await?;
    let moment = now().replace(' ', "T");
    for r in &mut rows {
        for k in ["created_at", "last_used_at", "expires_at", "revoked_at"] {
            r[k] = iso(&r[k]);
        }
        r["status"] = json!(if !r["revoked_at"].is_null() {
            "revoked"
        } else if r["expires_at"].as_str().unwrap_or("") <= moment.as_str() {
            "expired"
        } else {
            "active"
        });
    }
    Ok(Json(json!({"items":rows})))
}
pub async fn revoke(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    s.config.writable()?;
    if s.db
        .count(
            "SELECT COUNT(*) FROM device_sessions WHERE id=?",
            &[json!(id)],
        )
        .await?
        == 0
    {
        return Err(ApiError::missing("设备不存在"));
    }
    s.db.execute(
        "UPDATE device_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        &[json!(now()), json!(id)],
    )
    .await?;
    Ok(Json(json!({"ok":true})))
}
pub async fn disconnect(
    State(s): State<AppState>,
    device: Option<Extension<DeviceId>>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let id = device
        .ok_or(ApiError(
            axum::http::StatusCode::UNAUTHORIZED,
            "需要设备凭据",
        ))?
        .0
        .0;
    s.db.execute(
        "UPDATE device_sessions SET revoked_at=? WHERE id=?",
        &[json!(now()), json!(id)],
    )
    .await?;
    Ok(Json(json!({"ok":true})))
}
