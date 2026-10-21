use crate::{
    ApiJson, AppState,
    db::{iso, json_column, now},
    error::{ApiError, Result},
    library::placeholders,
    secrets,
};
use axum::{
    Json,
    extract::{Path, State},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap, HashSet};
pub const ROLE_KEY: &str = "pixiv_account_roles";
pub const FIELDS: [&str; 4] = [
    "download_account_id",
    "details_account_id",
    "check_account_id",
    "execution_account_id",
];
pub const PLATFORMS: [(&str, &str); 5] = [
    ("pixiv", "Pixiv"),
    ("ehentai", "E-Hentai"),
    ("picacg", "哔咔漫画"),
    ("jm", "JM 天堂"),
    ("kemono", "Kemono"),
];

#[derive(Default, Clone, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Roles {
    pub enabled: bool,
    pub main_account_id: Option<String>,
    pub content_account_id: Option<String>,
    pub content_account_ids: Option<Vec<String>>,
    pub manual_account_ids: Vec<String>,
    pub retry_account_ids: Vec<String>,
    pub detail_account_ids: Vec<String>,
    pub check_account_ids: Vec<String>,
    pub account_limits: BTreeMap<String, u8>,
}
impl Roles {
    pub fn pool(&self) -> Vec<String> {
        self.content_account_ids
            .clone()
            .unwrap_or_else(|| self.content_account_id.clone().into_iter().collect())
    }
    pub fn normalize(mut self) -> Result<Self> {
        let pool = self.pool();
        self.content_account_id = pool.first().cloned();
        self.content_account_ids = Some(pool);
        for ids in [
            self.pool(),
            self.manual_account_ids.clone(),
            self.retry_account_ids.clone(),
            self.detail_account_ids.clone(),
            self.check_account_ids.clone(),
        ] {
            if ids.iter().any(|s| s.is_empty() || s.len() > 36)
                || ids.iter().collect::<HashSet<_>>().len() != ids.len()
            {
                return Err(ApiError::bad("账号功能不能重复选择，账号 ID 长度须为 1–36"));
            }
        }
        if self
            .main_account_id
            .as_ref()
            .is_some_and(|v| v.is_empty() || v.len() > 36)
            || self
                .account_limits
                .iter()
                .any(|(id, n)| id.is_empty() || id.len() > 36 || !(1..=8).contains(n))
        {
            return Err(ApiError::invalid("账号并发须在 1–8 之间"));
        }
        Ok(self)
    }
    pub fn limit(&self, id: &str) -> usize {
        usize::from(*self.account_limits.get(id).unwrap_or(&2))
    }
    pub fn assignment_pools(
        &self,
        job: &Value,
        fallback: Option<&str>,
    ) -> BTreeMap<&'static str, Vec<String>> {
        let p = json_column(&job["payload"]);
        let kind = job["kind"].as_str().unwrap_or("");
        if !self.enabled {
            return BTreeMap::from([(
                "execution_account_id",
                p["account_id"]
                    .as_str()
                    .or(fallback)
                    .map(str::to_owned)
                    .into_iter()
                    .collect(),
            )]);
        }
        let mut pools = BTreeMap::new();
        if matches!(kind, "import" | "import_content") {
            let novel = is_novel(&p);
            let body = kind == "import_content" || p["download"] != false;
            let manual = body && p["download_origin"] == "manual";
            if manual {
                pools.insert("download_account_id", self.manual_account_ids.clone());
            } else if !novel && body {
                pools.insert("download_account_id", self.pool());
            }
            if (kind == "import" || novel) && !(manual && novel) {
                pools.insert("details_account_id", self.detail_account_ids.clone());
            }
        } else {
            let profile = kind == "author_profile"
                && p["target"].as_str().unwrap_or("").to_lowercase() != "me";
            pools.insert(
                "check_account_id",
                if profile {
                    self.check_account_ids.clone()
                } else {
                    self.main_account_id.clone().into_iter().collect()
                },
            );
        }
        if p["retry_queue"] == true {
            for candidates in pools.values_mut() {
                candidates.retain(|id| self.retry_account_ids.contains(id));
            }
        }
        pools
    }
}
pub fn is_novel(p: &Value) -> bool {
    p["remote_id"]
        .as_str()
        .is_some_and(|s| s.starts_with("novel:") || s.contains("/novel/show.php?"))
}
pub async fn roles(s: &AppState) -> Result<Roles> {
    let v = secrets::get(s, ROLE_KEY)
        .await?
        .unwrap_or_else(|| json!({"enabled":false}));
    let roles: Roles = serde_json::from_value(v)
        .map_err(|_| ApiError::bad("Pixiv 账号分工配置无法读取，请重新保存"))?;
    let roles = roles.normalize()?;
    if roles.enabled
        && (roles.main_account_id.is_none()
            || roles.pool().is_empty()
                && roles.manual_account_ids.is_empty()
                && roles.detail_account_ids.is_empty()
                && roles.check_account_ids.is_empty()
            || roles
                .main_account_id
                .as_ref()
                .is_some_and(|id| roles.pool().contains(id)))
    {
        return Err(ApiError::bad("Pixiv 账号分工配置无法读取，请重新保存"));
    }
    Ok(roles)
}
pub async fn rows(s: &AppState) -> Result<Vec<Value>> {
    s.db.query(
        "SELECT * FROM source_accounts WHERE platform='pixiv' ORDER BY created_at,id",
        &[],
    )
    .await
}
fn account_status(id: Option<&str>, records: &HashMap<String, Value>) -> Value {
    let row = id.and_then(|id| records.get(id));
    json!({"id":id,"label":row.map(|r|r["label"].clone()),"status":if id.is_none(){"unconfigured"}else if row.is_none(){"missing"}else if row.is_some_and(|v|!v["verified_at"].is_null()){"verified"}else{"unverified"},"verified_at":row.map(|r|iso(&r["verified_at"]))})
}
pub async fn public_roles(s: &AppState) -> Result<Value> {
    let r = roles(s).await?;
    let records = rows(s).await?;
    let map: HashMap<_, _> = records
        .iter()
        .map(|v| (v["id"].as_str().unwrap().to_owned(), v.clone()))
        .collect();
    let mut counts: HashMap<(String, String), i64> = HashMap::new();
    let known: Vec<_> = records.iter().map(|v| v["id"].clone()).collect();
    if !known.is_empty() {
        let columns = [
            "download_account_id",
            "stats_details_account_id",
            "stats_check_account_id",
            "stats_execution_account_id",
        ];
        let mut sql = Vec::new();
        let mut args = Vec::new();
        for col in columns {
            sql.push(format!("SELECT {col} AS account_id,status,COUNT(*) AS n FROM jobs WHERE {col} IN ({}) AND status IN ('running','completed','failed') GROUP BY {col},status",placeholders(known.len())));
            args.extend(known.clone());
        }
        for row in s.db.query(&sql.join(" UNION ALL "), &args).await? {
            *counts
                .entry((
                    row["account_id"].as_str().unwrap().to_owned(),
                    row["status"].as_str().unwrap().to_owned(),
                ))
                .or_default() += row["n"].as_i64().unwrap_or(0);
        }
    }
    let mut public = serde_json::to_value(&r).unwrap();
    let empty = account_status(None, &map);
    public["main"] = r
        .main_account_id
        .as_deref()
        .map(|id| account_status(Some(id), &map))
        .unwrap_or(empty.clone());
    public["content"] = r
        .content_account_id
        .as_deref()
        .map(|id| account_status(Some(id), &map))
        .unwrap_or(empty);
    public["contents"] = json!(
        r.pool()
            .iter()
            .map(|id| account_status(Some(id), &map))
            .collect::<Vec<_>>()
    );
    public["download_accounts"] = json!(
        records
            .iter()
            .map(|record| {
                let id = record["id"].as_str().unwrap();
                let mut a = account_status(Some(id), &map);
                a["in_pool"] = json!(r.pool().iter().any(|i| i == id));
                a["concurrency"] = json!(r.limit(id));
                for status in ["running", "completed", "failed"] {
                    a[status] = json!(
                        counts
                            .get(&(id.to_owned(), status.to_owned()))
                            .copied()
                            .unwrap_or(0)
                    );
                }
                a
            })
            .collect::<Vec<_>>()
    );
    let mut ids: HashSet<String> = r
        .pool()
        .into_iter()
        .chain(r.manual_account_ids.iter().cloned())
        .chain(r.retry_account_ids.iter().cloned())
        .chain(r.detail_account_ids.iter().cloned())
        .chain(r.check_account_ids.iter().cloned())
        .chain(r.main_account_id.iter().cloned())
        .collect();
    ids.retain(|id| !map.contains_key(id));
    let mut missing: Vec<_> = ids.into_iter().collect();
    missing.sort();
    public["missing_account_ids"] = json!(missing);
    Ok(public)
}
pub async fn get_roles(State(s): State<AppState>) -> Result<Json<Value>> {
    Ok(Json(public_roles(&s).await?))
}
pub async fn set_roles(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<Value>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    if v["enabled"].as_bool().is_none() {
        return Err(ApiError::invalid("账号分工需要 enabled 布尔字段"));
    }
    let mut r: Roles =
        serde_json::from_value(v.clone()).map_err(|_| ApiError::invalid("账号分工格式无效"))?;
    let mut tx = s.db.begin().await?;
    let _ = crate::queue::locked(&s, &mut tx).await?;
    let previous = tx
        .query(
            "SELECT value_encrypted FROM network_settings WHERE `key`=?",
            &[json!(ROLE_KEY)],
        )
        .await?
        .pop()
        .and_then(|r| {
            secrets::decrypt(
                &s.config.secret,
                r["value_encrypted"].as_str().unwrap_or(""),
            )
            .ok()
        })
        .and_then(|v| serde_json::from_value::<Roles>(v).ok());
    if let Some(previous) = previous {
        for key in [
            "manual_account_ids",
            "retry_account_ids",
            "detail_account_ids",
            "check_account_ids",
            "account_limits",
        ] {
            if v.get(key).is_none() {
                let mut merged = serde_json::to_value(&r).unwrap();
                merged[key] = serde_json::to_value(&previous).unwrap()[key].clone();
                r = serde_json::from_value(merged).unwrap();
            }
        }
    }
    r = r.normalize()?;
    if r.enabled
        && (r.main_account_id.is_none()
            || r.pool().is_empty()
                && r.manual_account_ids.is_empty()
                && r.detail_account_ids.is_empty()
                && r.check_account_ids.is_empty())
    {
        return Err(ApiError::bad("启用分工需要选择主账号和至少一种副账号功能"));
    }
    if r.main_account_id
        .as_ref()
        .is_some_and(|id| r.pool().contains(id))
    {
        return Err(ApiError::bad("主账号与下载副账号必须选择不同账号"));
    }
    let accounts = tx
        .query(
            &format!(
                "SELECT * FROM source_accounts WHERE platform='pixiv' ORDER BY id{}",
                if s.db.mysql() { " FOR UPDATE" } else { "" }
            ),
            &[],
        )
        .await?;
    let records: HashMap<_, _> = accounts
        .into_iter()
        .map(|a| (a["id"].as_str().unwrap().to_owned(), a))
        .collect();
    let selected: HashSet<_> = r
        .pool()
        .into_iter()
        .chain(r.manual_account_ids.iter().cloned())
        .chain(r.retry_account_ids.iter().cloned())
        .chain(r.detail_account_ids.iter().cloned())
        .chain(r.check_account_ids.iter().cloned())
        .chain(r.main_account_id.iter().cloned())
        .collect();
    if r.enabled
        && selected
            .iter()
            .chain(r.account_limits.keys())
            .any(|id| !records.contains_key(id))
    {
        return Err(ApiError::bad("所选 Pixiv 账号不存在，请刷新账号列表"));
    }
    if r.enabled {
        let mut tokens = HashSet::new();
        let mut users = HashSet::new();
        for id in &selected {
            let credentials = secrets::decrypt(
                &s.config.secret,
                records[id]["credentials_encrypted"].as_str().unwrap_or(""),
            )?;
            for (key, set) in [("refresh_token", &mut tokens), ("user_id", &mut users)] {
                let value = credentials[key]
                    .as_str()
                    .map(str::to_owned)
                    .or_else(|| credentials[key].as_u64().map(|v| v.to_string()))
                    .unwrap_or_default();
                if !value.is_empty() && !set.insert(value) {
                    return Err(ApiError::bad(
                        "多个记录对应同一个 Pixiv 账号，请切换账号重新授权",
                    ));
                }
            }
        }
    } else {
        r.main_account_id = r.main_account_id.filter(|id| records.contains_key(id));
        r.content_account_ids = Some(
            r.pool()
                .into_iter()
                .filter(|id| records.contains_key(id))
                .collect(),
        );
        r.content_account_id = r.pool().first().cloned();
        r.manual_account_ids.retain(|id| records.contains_key(id));
        r.retry_account_ids.retain(|id| records.contains_key(id));
        r.detail_account_ids.retain(|id| records.contains_key(id));
        r.check_account_ids.retain(|id| records.contains_key(id));
        r.account_limits.retain(|id, _| records.contains_key(id));
    }
    secrets::put_tx(&s, &mut tx, ROLE_KEY, &serde_json::to_value(&r).unwrap()).await?;
    tx.commit().await?;
    Ok(Json(public_roles(&s).await?))
}
#[derive(Deserialize)]
pub struct AccountInput {
    label: String,
    credentials: BTreeMap<String, String>,
}
pub fn validate_credentials(platform: &str, c: &Value) -> Result<()> {
    let has = |key: &str| c[key].as_str().is_some_and(|v| !v.trim().is_empty());
    let valid = match platform {
        "pixiv" => has("refresh_token"),
        "picacg" => has("token"),
        "ehentai" => has("ipb_member_id") && has("ipb_pass_hash"),
        "jm" => has("AVS"),
        "kemono" => true,
        _ => return Err(ApiError::missing("不支持的平台")),
    };
    if !valid {
        return Err(ApiError::bad(
            "平台账号会话不完整，请重新登录或导入有效会话",
        ));
    }
    Ok(())
}
pub async fn save(
    s: &AppState,
    platform: &str,
    label: &str,
    credentials: &Value,
    verified: bool,
) -> Result<Value> {
    s.config.writable()?;
    if !PLATFORMS.iter().any(|(p, _)| *p == platform) {
        return Err(ApiError::missing("不支持的平台"));
    }
    let label = label.trim();
    if label.is_empty() || label.chars().count() > 191 {
        return Err(ApiError::invalid("账号名称不能为空或超过 191 字符"));
    }
    validate_credentials(platform, credentials)?;
    let id = uuid::Uuid::new_v4().to_string();
    let t = now();
    s.db.execute("INSERT INTO source_accounts(id,platform,label,credentials_encrypted,verified_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",&[json!(id),json!(platform),json!(label),json!(secrets::encrypt(&s.config.secret,credentials)),if verified{json!(t)}else{Value::Null},json!(t),json!(t)]).await?;
    Ok(
        json!({"id":id,"label":label,"platform":platform,"auth_status":if verified{"verified"}else{"unverified"},"verified_at":if verified{iso(&json!(t))}else{Value::Null}}),
    )
}
pub async fn add(
    State(s): State<AppState>,
    Path(platform): Path<String>,
    ApiJson(v): ApiJson<AccountInput>,
) -> Result<Json<Value>> {
    if v.credentials.len() > 20 || v.credentials.values().map(String::len).sum::<usize>() > 16000 {
        return Err(ApiError::bad("账号凭据过长"));
    }
    Ok(Json(
        save(&s, &platform, &v.label, &json!(v.credentials), false).await?,
    ))
}
pub async fn remove(
    State(s): State<AppState>,
    Path((platform, id)): Path<(String, String)>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    let _ = crate::queue::locked(&s, &mut tx).await?;
    if tx
        .query(
            "SELECT id FROM source_accounts WHERE id=? AND platform=?",
            &[json!(id), json!(platform)],
        )
        .await?
        .is_empty()
    {
        return Err(ApiError::missing("账号不存在"));
    }
    tx.execute("UPDATE subscriptions SET enabled=0,account_id=NULL,last_error=?,updated_at=? WHERE account_id=?",&[json!("账号已删除，请重新指定账号并启用订阅"),json!(now()),json!(id)]).await?;
    tx.execute("DELETE FROM source_accounts WHERE id=?", &[json!(id)])
        .await?;
    tx.commit().await?;
    Ok(Json(json!({"ok":true})))
}
pub async fn credentials(s: &AppState, platform: &str, id: Option<&str>) -> Result<Value> {
    let a = if let Some(id) = id {
        s.db.one(
            "SELECT * FROM source_accounts WHERE id=? AND platform=?",
            &[json!(id), json!(platform)],
        )
        .await?
    } else {
        s.db.one(
            "SELECT * FROM source_accounts WHERE platform=? ORDER BY created_at,id LIMIT 1",
            &[json!(platform)],
        )
        .await?
    };
    match a {
        Some(v) => secrets::decrypt(
            &s.config.secret,
            v["credentials_encrypted"].as_str().unwrap_or(""),
        ),
        None if id.is_some() => Err(ApiError::bad("账号不存在或与内容平台不匹配")),
        None => Ok(json!({})),
    }
}
pub async fn resolve(
    s: &AppState,
    requested: Option<&str>,
    purpose: &str,
    pinned: bool,
    retry: bool,
) -> Result<Option<String>> {
    let r = roles(s).await?;
    if !r.enabled {
        let id = if let Some(id) = requested {
            Some(id.to_owned())
        } else {
            s.db.one("SELECT id FROM source_accounts WHERE platform='pixiv' ORDER BY created_at,id LIMIT 1",&[]).await?.and_then(|v|v["id"].as_str().map(str::to_owned))
        };
        if retry
            && !id
                .as_ref()
                .is_some_and(|id| r.retry_account_ids.contains(id))
        {
            return Err(ApiError::bad("Pixiv 执行账号未获得重试权限"));
        }
        return Ok(id);
    }
    let pool = match purpose {
        "content" => r.pool(),
        "manual" => r.manual_account_ids.clone(),
        "details" => r.detail_account_ids.clone(),
        "checks" => r.check_account_ids.clone(),
        "metadata" => r.main_account_id.clone().into_iter().collect(),
        _ => return Err(ApiError::bad("账号功能无效")),
    };
    let id = if purpose == "metadata" {
        if pinned && requested != r.main_account_id.as_deref() {
            return Err(ApiError::bad(
                "Pixiv 主账号已更改，请重新获取目录；旧账号目录未改写",
            ));
        }
        r.main_account_id.clone()
    } else if pinned {
        requested.map(str::to_owned)
    } else {
        pool.iter()
            .find(|id| !retry || r.retry_account_ids.contains(id))
            .cloned()
    };
    let id = id
        .filter(|id| pool.contains(id))
        .ok_or_else(|| ApiError::bad("Pixiv 执行账号未配置或已退出对应功能，请重新分配任务"))?;
    if retry && !r.retry_account_ids.contains(&id) {
        return Err(ApiError::bad(
            "Pixiv 执行账号未同时获得原任务功能与重试权限",
        ));
    }
    if s.db
        .one(
            "SELECT id FROM source_accounts WHERE id=? AND platform='pixiv'",
            &[json!(id)],
        )
        .await?
        .is_none()
    {
        return Err(ApiError::bad("Pixiv 执行账号已移除，请重新配置后重试"));
    }
    Ok(Some(id))
}
pub fn choose_assignment(
    r: &Roles,
    job: &Value,
    records: &HashSet<String>,
    busy: &HashMap<String, usize>,
    cursor: &mut usize,
    fallback: Option<&str>,
) -> Option<Value> {
    let pools = r.assignment_pools(job, fallback);
    let mut p = json_column(&job["payload"]);
    for field in FIELDS {
        if !pools.contains_key(field) {
            p.as_object_mut()?.remove(field);
        }
    }
    for (field, pool) in pools {
        if pool.is_empty() {
            continue;
        }
        let pinned = p[field].as_str();
        let candidates: Vec<_> = if let Some(id) = pinned {
            vec![id.to_owned()]
        } else {
            let existing: Vec<_> = pool
                .iter()
                .filter(|id| records.contains(*id))
                .cloned()
                .collect();
            if existing.is_empty() { pool } else { existing }
        };
        let available: Vec<_> = candidates
            .into_iter()
            .filter(|id| busy.get(id).copied().unwrap_or(0) < r.limit(id))
            .collect();
        if available.is_empty() {
            return None;
        }
        let start = *cursor % available.len();
        let chosen = available[start..]
            .iter()
            .chain(available[..start].iter())
            .min_by_key(|id| busy.get(*id).copied().unwrap_or(0))?;
        *cursor += 1;
        p[field] = json!(chosen);
    }
    Some(p)
}
pub fn validate_retry(
    r: &Roles,
    job: &Value,
    records: &HashSet<String>,
    fallback: Option<&str>,
) -> Result<()> {
    let p = json_column(&job["payload"]);
    if p["retry_queue"] != true {
        return Ok(());
    }
    for (field, pool) in r.assignment_pools(job, fallback) {
        let Some(id) = p[field].as_str() else {
            return Err(ApiError::bad("执行账号未同时获得原任务功能与重试权限"));
        };
        if !pool.iter().any(|i| i == id)
            || !r.retry_account_ids.iter().any(|i| i == id)
            || !records.contains(id)
        {
            return Err(ApiError::bad("执行账号未同时获得原任务功能与重试权限"));
        }
    }
    Ok(())
}
