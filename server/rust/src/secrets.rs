//! Compatible with app.integrations.credentials; never return raw values to UI.
use crate::{
    AppState,
    db::{Tx, now},
    error::{ApiError, Result},
};
use base64::{Engine, engine::general_purpose::URL_SAFE};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
fn cipher(secret: &str) -> fernet::Fernet {
    let key = Sha256::digest(format!("library:accounts:v1:{secret}").as_bytes());
    fernet::Fernet::new(&URL_SAFE.encode(key)).expect("SHA256 produces a Fernet key")
}
pub fn encrypt(secret: &str, value: &Value) -> String {
    cipher(secret).encrypt(value.to_string().as_bytes())
}
pub fn decrypt(secret: &str, value: &str) -> Result<Value> {
    let data = cipher(secret)
        .decrypt(value)
        .map_err(|_| ApiError::bad("账号凭据无法解密；请使用原密钥恢复或重新添加账号"))?;
    let v: Value =
        serde_json::from_slice(&data).map_err(|_| ApiError::bad("加密配置格式无效，请重新保存"))?;
    if !v.is_object() {
        return Err(ApiError::bad("加密配置格式无效，请重新保存"));
    }
    Ok(v)
}
pub async fn get(s: &AppState, key: &str) -> Result<Option<Value>> {
    mapped(s, key, Value::clone).await
}
pub type Cache = tokio::sync::RwLock<std::collections::HashMap<String, (String, Value)>>;
pub async fn mapped<T>(s: &AppState, key: &str, map: impl Fn(&Value) -> T) -> Result<Option<T>> {
    // Fernet's prefix includes the random IV. This bounded version probe sees
    // changes immediately across workers without transferring/decrypting a
    // multi-megabyte subscription document for every outgoing request.
    let probe=s.db.one("SELECT SUBSTR(value_encrypted,1,128) AS prefix,LENGTH(value_encrypted) AS n FROM network_settings WHERE `key`=?",&[json!(key)]).await?;
    let Some(probe) = probe else {
        s.secret_cache.write().await.remove(key);
        return Ok(None);
    };
    let version = format!("{}:{}", probe["prefix"].as_str().unwrap_or(""), probe["n"]);
    {
        let cache = s.secret_cache.read().await;
        if let Some((cached, value)) = cache.get(key)
            && cached == &version
        {
            return Ok(Some(map(value)));
        }
    }
    let full =
        s.db.one(
            "SELECT value_encrypted FROM network_settings WHERE `key`=?",
            &[json!(key)],
        )
        .await?;
    let Some(full) = full else { return Ok(None) };
    let encrypted = full["value_encrypted"].as_str().unwrap_or("");
    let value = decrypt(&s.config.secret, encrypted)?;
    let actual = format!(
        "{}:{}",
        encrypted.chars().take(128).collect::<String>(),
        encrypted.len()
    );
    let result = map(&value);
    let mut cache = s.secret_cache.write().await;
    if cache.len() >= 128 {
        cache.clear();
    }
    cache.insert(key.into(), (actual, value));
    Ok(Some(result))
}
pub async fn put_tx(s: &AppState, tx: &mut Tx, key: &str, value: &Value) -> Result<()> {
    let sql = format!(
        "INSERT INTO network_settings(`key`,value_encrypted,created_at,updated_at) VALUES(?,?,?,?) {}",
        s.db.upsert("value_encrypted,updated_at", "key")
    );
    tx.execute(
        &sql,
        &[
            json!(key),
            json!(encrypt(&s.config.secret, value)),
            json!(now()),
            json!(now()),
        ],
    )
    .await?;
    Ok(())
}
pub async fn put(s: &AppState, key: &str, value: &Value) -> Result<()> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    put_tx(s, &mut tx, key, value).await?;
    tx.commit().await
}
/// Preserve fingerprints used by persisted Python translation jobs.
pub fn legacy_json(v: &Value) -> String {
    match v {
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!(
                    "{}: {}",
                    legacy_json(&Value::String(k.clone())),
                    legacy_json(v)
                ))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(legacy_json).collect::<Vec<_>>().join(", ")
        ),
        Value::String(_) => {
            let mut text = String::new();
            for c in v.to_string().chars() {
                if c.is_ascii() {
                    text.push(c);
                } else {
                    for code in c.encode_utf16(&mut [0u16; 2]) {
                        text.push_str(&format!("\\u{code:04x}"));
                    }
                }
            }
            text
        }
        _ => v.to_string(),
    }
}
pub fn fingerprint(v: &Value) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(legacy_json(v)))
}
