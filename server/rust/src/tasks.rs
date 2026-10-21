//! Transactional dispatch shared by native workers. No platform request or wait
//! occurs while holding QueueControl, a transaction, or an account reservation lock.
use crate::{
    AppState,
    accounts::{self, Roles},
    db::{Tx, json_column, now, truth},
    error::{ApiError, Result},
    queue, secrets,
};
use rand::Rng;
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    future::Future,
    pin::Pin,
    sync::Arc,
    time::Duration,
};
use tokio_util::sync::CancellationToken;
pub const LEASE_SECONDS: i64 = 300;
#[derive(Debug, thiserror::Error)]
pub enum TaskError {
    #[error("队列已暂停或服务正在退出")]
    Paused,
    #[error("任务已取消或租约已失效")]
    Cancelled,
    #[error("{0}")]
    Failed(#[from] ApiError),
}
impl From<sqlx::Error> for TaskError {
    fn from(e: sqlx::Error) -> Self {
        Self::Failed(e.into())
    }
}
impl From<std::io::Error> for TaskError {
    fn from(e: std::io::Error) -> Self {
        Self::Failed(e.into())
    }
}
pub type TaskResult<T> = std::result::Result<T, TaskError>;
pub fn date(v: &Value) -> Option<chrono::NaiveDateTime> {
    let s = v.as_str()?.trim_end_matches('Z').replace('T', " ");
    chrono::NaiveDateTime::parse_from_str(&s, "%Y-%m-%d %H:%M:%S%.f").ok()
}
fn future_time(seconds: i64) -> String {
    (chrono::Utc::now() + chrono::Duration::seconds(seconds))
        .naive_utc()
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string()
}
pub fn lane(job: &Value) -> String {
    let p = json_column(&job["payload"]);
    if p["retry_queue"] == true {
        "retry".to_owned()
    } else {
        queue::original_lane(job["kind"].as_str().unwrap_or(""), &p).to_owned()
    }
}
pub fn json_text(s: &AppState, column: &str, key: &str) -> String {
    // Both identifiers are compile-time call-site constants, never request text.
    assert!(
        column
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_')
    );
    assert!(key.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_'));
    if s.db.mysql() {
        format!("JSON_UNQUOTE(JSON_EXTRACT({column},'$.{key}'))")
    } else {
        format!("CAST(json_extract({column},'$.{key}') AS TEXT)")
    }
}
pub async fn roles_tx(s: &AppState, tx: &mut Tx) -> Result<Roles> {
    let row = tx
        .query(
            "SELECT value_encrypted FROM network_settings WHERE `key`=?",
            &[json!(accounts::ROLE_KEY)],
        )
        .await?
        .pop();
    let value = row
        .map(|r| {
            secrets::decrypt(
                &s.config.secret,
                r["value_encrypted"].as_str().unwrap_or(""),
            )
        })
        .transpose()?
        .unwrap_or_else(|| json!({"enabled":false}));
    serde_json::from_value::<Roles>(value)
        .map_err(|_| ApiError::bad("Pixiv 账号分工配置无法读取，请重新保存"))?
        .normalize()
}
async fn resources(
    s: &AppState,
    tx: &mut Tx,
) -> Result<(
    Roles,
    HashSet<String>,
    HashMap<String, usize>,
    Option<String>,
)> {
    let roles = roles_tx(s, tx).await?;
    let records = tx
        .query(
            "SELECT id FROM source_accounts WHERE platform='pixiv' ORDER BY created_at,id",
            &[],
        )
        .await?;
    let fallback = records
        .first()
        .and_then(|v| v["id"].as_str())
        .map(str::to_owned);
    let ids = records
        .into_iter()
        .filter_map(|v| v["id"].as_str().map(str::to_owned))
        .collect();
    let mut busy = HashMap::new();
    for row in tx.query("SELECT download_account_id,details_account_id,check_account_id,execution_account_id FROM jobs WHERE status='running'",&[]).await?{let unique:HashSet<_>=accounts::FIELDS.iter().filter_map(|f|row[*f].as_str()).collect();for id in unique{*busy.entry(id.to_owned()).or_insert(0)+=1;}}
    Ok((roles, ids, busy, fallback))
}
fn available_filter(
    s: &AppState,
    r: &Roles,
    busy: &HashMap<String, usize>,
    fallback: Option<&str>,
) -> (String, Vec<Value>) {
    let platform = json_text(s, "payload", "platform");
    let remote = json_text(s, "payload", "remote_id");
    let dl = json_text(s, "payload", "download");
    let origin = json_text(s, "payload", "download_origin");
    let retry = format!(
        "COALESCE({},'false') IN ('true','1')",
        json_text(s, "payload", "retry_queue")
    );
    let target = json_text(s, "payload", "target");
    let pixiv = format!("{platform}='pixiv'");
    let novel = format!("({remote} LIKE 'novel:%' OR {remote} LIKE 'https://%/novel/show.php?%')");
    let manual = format!("COALESCE({origin},'')='manual'");
    let body = format!(
        "(kind='import_content' OR(kind='import' AND({dl} IS NULL OR {dl} IN ('true','1'))))"
    );
    let full: HashSet<_> = busy
        .iter()
        .filter(|(id, n)| **n >= r.limit(id))
        .map(|(id, _)| id.as_str())
        .collect();
    if full.is_empty() {
        return ("1=1".to_owned(), vec![]);
    }
    let mut clauses = vec![];
    let mut args = vec![];
    if !r.enabled {
        let account = json_text(s, "payload", "account_id");
        let identity = format!("COALESCE(NULLIF(execution_account_id,''),NULLIF({account},''),?)");
        for id in &full {
            let retry_allowed = r.retry_account_ids.iter().any(|i| i == id);
            clauses.push(format!("NOT (({pixiv} OR kind IN ('author_profile','author_feed','series_sync')) AND {identity}=? AND (NOT({retry}) OR ?))"));
            args.extend([
                fallback.map(|v| json!(v)).unwrap_or(Value::Null),
                json!(id),
                json!(retry_allowed),
            ]);
        }
    } else {
        let specs = [
            (
                "download_account_id",
                r.pool(),
                format!("{pixiv} AND {body} AND NOT({novel}) AND NOT({manual})"),
            ),
            (
                "download_account_id",
                r.manual_account_ids.clone(),
                format!("{pixiv} AND {body} AND {manual}"),
            ),
            (
                "details_account_id",
                r.detail_account_ids.clone(),
                format!(
                    "{pixiv} AND(kind='import' OR(kind='import_content' AND {novel})) AND NOT({body} AND {manual} AND {novel})"
                ),
            ),
            (
                "check_account_id",
                r.check_account_ids.clone(),
                format!("kind='author_profile' AND LOWER(COALESCE({target},''))<>'me'"),
            ),
            (
                "check_account_id",
                r.main_account_id.clone().into_iter().collect(),
                format!(
                    "kind IN ('author_feed','series_sync') OR(kind='author_profile' AND LOWER(COALESCE({target},''))='me') OR(kind IN ('source_sync','pixiv_bookmark') AND {pixiv})"
                ),
            ),
        ];
        for (field, pool, need) in specs {
            for (is_retry, pool) in [
                (false, pool.clone()),
                (
                    true,
                    pool.into_iter()
                        .filter(|id| r.retry_account_ids.contains(id))
                        .collect::<Vec<_>>(),
                ),
            ] {
                let scope = if is_retry {
                    retry.clone()
                } else {
                    format!("NOT({retry})")
                };
                let full_pool: Vec<_> = pool
                    .iter()
                    .filter(|id| full.contains(id.as_str()))
                    .collect();
                if !full_pool.is_empty() {
                    clauses.push(format!(
                        "NOT(({need}) AND({scope}) AND COALESCE({field},'') IN ({}))",
                        crate::library::placeholders(full_pool.len())
                    ));
                    args.extend(full_pool.into_iter().map(|id| json!(id)));
                }
                if !pool.is_empty() && pool.iter().all(|id| full.contains(id.as_str())) {
                    clauses.push(format!(
                        "({field} IS NOT NULL OR NOT(({need}) AND({scope})))"
                    ));
                }
            }
        }
    }
    (
        if clauses.is_empty() {
            "1=1".to_owned()
        } else {
            clauses.join(" AND ")
        },
        args,
    )
}
pub(crate) async fn sync_state(tx: &mut Tx, id: &str) -> Result<()> {
    let Some(j) = tx
        .query(
            "SELECT id,kind,status,payload,error FROM jobs WHERE id=?",
            &[json!(id)],
        )
        .await?
        .pop()
    else {
        return Ok(());
    };
    if !matches!(j["kind"].as_str(), Some("import" | "import_content")) {
        return Ok(());
    }
    let p = json_column(&j["payload"]);
    let Some(item) = p["item_id"].as_str() else {
        return Ok(());
    };
    let status = match j["status"].as_str() {
        Some("failed") => "failed",
        Some("cancelled") => "metadata",
        Some("queued") => "queued",
        Some("running") => "downloading",
        _ => return Ok(()),
    };
    tx.execute("UPDATE items SET content_state=?,content_error=?,updated_at=? WHERE id=? AND content_job_id=? AND NOT EXISTS(SELECT 1 FROM assets WHERE assets.item_id=items.id)",&[json!(status),if status=="failed"{j["error"].clone()}else{Value::Null},json!(now()),json!(item),json!(id)]).await?;
    Ok(())
}
pub async fn recover(s: &AppState, tx: &mut Tx) -> Result<usize> {
    let stale=tx.query("SELECT id FROM jobs WHERE status='running' AND(lease_until IS NULL OR lease_until<?) ORDER BY lease_until,id LIMIT 100",&[json!(now())]).await?;
    for j in &stale {
        tx.execute("UPDATE jobs SET status='queued',worker_id=NULL,lease_until=NULL,updated_at=? WHERE id=? AND status='running'",&[json!(now()),j["id"].clone()]).await?;
        sync_state(tx, j["id"].as_str().unwrap()).await?;
    }
    let _ = s;
    Ok(stale.len())
}
#[derive(Clone)]
pub struct Claim {
    pub job: Value,
    pub owner: String,
    pub lane: String,
}
pub async fn claim(s: &AppState, key: &str) -> Result<Option<Claim>> {
    s.config.writable()?;
    if !queue::LANES.iter().any(|(k, _)| *k == key) {
        return Err(ApiError::bad("队列不存在"));
    }
    let proxy_ready = if matches!(
        key,
        "download" | "details" | "checks" | "manual_download" | "retry"
    ) {
        crate::network::managed_ready(s).await?
    } else {
        true
    };
    let transport_filter = if proxy_ready {
        "1=1"
    } else {
        "kind NOT IN ('import','import_content','source_sync','series_sync','author_profile','author_feed','pixiv_bookmark')"
    };
    let mut tx = s.db.begin().await?;
    let control = queue::locked(s, &mut tx).await?;
    recover(s, &mut tx).await?;
    let setting = queue::lane(&control, key);
    if truth(&setting["paused"]) || truth(&control["paused"]) && queue::collection(key) {
        tx.commit().await?;
        return Ok(None);
    }
    let running = tx
        .query(
            "SELECT COUNT(*) AS n FROM jobs WHERE queue_lane=? AND status='running'",
            &[json!(key)],
        )
        .await?;
    if running[0]["n"].as_i64().unwrap_or(0) >= setting["concurrency"].as_i64().unwrap_or(1) {
        tx.commit().await?;
        return Ok(None);
    }
    let resource = match resources(s, &mut tx).await {
        Ok(v) => Some(v),
        Err(e) if e.0 == axum::http::StatusCode::BAD_REQUEST => None,
        Err(e) => return Err(e),
    };
    let (filter, mut args) = resource
        .as_ref()
        .map(|(r, _, b, f)| available_filter(s, r, b, f.as_deref()))
        .unwrap_or_else(|| ("1=1".to_owned(), vec![]));
    args.insert(0, json!(key));
    let (source_filter, source_args) = if queue::collection(key) {
        queue::source_filter(&control, &queue::source_expression(s))
    } else {
        ("1=1".to_owned(), vec![])
    };
    args.extend(source_args);
    let candidates=tx.query(&format!("SELECT * FROM jobs WHERE queue_lane=? AND status='queued' AND({filter}) AND({source_filter}) AND({transport_filter}) ORDER BY created_at,id LIMIT 64"),&args).await?;
    let mut cursor = control["download_account_cursor"].as_u64().unwrap_or(0) as usize;
    let mut chosen = None;
    for mut job in candidates {
        let p = json_column(&job["payload"]);
        let pixiv = p["platform"] == "pixiv"
            || matches!(
                job["kind"].as_str(),
                Some("author_profile" | "author_feed" | "series_sync")
            );
        if pixiv && let Some((r, records, busy, fallback)) = &resource {
            let Some(p) = accounts::choose_assignment(
                r,
                &job,
                records,
                busy,
                &mut cursor,
                fallback.as_deref(),
            ) else {
                continue;
            };
            job["payload"] = p;
        } else {
            job["payload"] = p;
        }
        chosen = Some(job);
        break;
    }
    let Some(mut job) = chosen else {
        tx.commit().await?;
        return Ok(None);
    };
    let owner = uuid::Uuid::new_v4().to_string();
    let t = now();
    let lease = future_time(LEASE_SECONDS);
    if tx.execute("UPDATE jobs SET status='running',worker_id=?,lease_until=?,attempts=attempts+1,error=NULL,progress=0,payload=?,updated_at=? WHERE id=? AND status='queued'",&[json!(owner),json!(lease),json!(job["payload"].to_string()),json!(t),job["id"].clone()]).await?!=1{tx.commit().await?;return Ok(None);}
    tx.execute(
        "UPDATE queue_control SET download_account_cursor=? WHERE id=1",
        &[json!(cursor)],
    )
    .await?;
    sync_state(&mut tx, job["id"].as_str().unwrap()).await?;
    tx.commit().await?;
    job["status"] = json!("running");
    job["worker_id"] = json!(owner);
    job["lease_until"] = json!(lease);
    Ok(Some(Claim {
        job,
        owner,
        lane: key.to_owned(),
    }))
}
#[derive(Clone)]
pub struct Context {
    pub state: AppState,
    pub id: String,
    pub owner: String,
    pub lane: String,
    pub stop: CancellationToken,
}
impl Context {
    pub fn new(state: AppState, c: &Claim, stop: CancellationToken) -> Self {
        Self {
            state,
            id: c.job["id"].as_str().unwrap().to_owned(),
            owner: c.owner.clone(),
            lane: c.lane.clone(),
            stop,
        }
    }
    pub async fn guard_tx(&self, tx: &mut Tx) -> TaskResult<Value> {
        if self.stop.is_cancelled() {
            return Err(TaskError::Paused);
        }
        let control = queue::locked(&self.state, tx).await?;
        let j = tx
            .query(
                &format!(
                    "SELECT * FROM jobs WHERE id=?{}",
                    if self.state.db.mysql() {
                        " FOR UPDATE"
                    } else {
                        ""
                    }
                ),
                &[json!(self.id)],
            )
            .await?
            .pop()
            .ok_or(TaskError::Cancelled)?;
        if j["status"] != "running"
            || j["worker_id"] != self.owner
            || date(&j["lease_until"]).is_none_or(|t| t <= chrono::Utc::now().naive_utc())
        {
            return Err(TaskError::Cancelled);
        }
        {
            if truth(&queue::lane(&control, &self.lane)["paused"])
                || truth(&control["paused"]) && queue::collection(&self.lane)
                || queue::collection(&self.lane)
                    && !queue::source_enabled(&control, &queue::job_source(&j))
            {
                return Err(TaskError::Paused);
            }
        }
        let p = json_column(&j["payload"]);
        if p["retry_queue"] == true
            && (p["platform"] == "pixiv"
                || matches!(
                    j["kind"].as_str(),
                    Some("author_profile" | "author_feed" | "series_sync")
                ))
        {
            let r = roles_tx(&self.state, tx).await?;
            let rows = tx
                .query(
                    "SELECT id FROM source_accounts WHERE platform='pixiv' ORDER BY created_at,id",
                    &[],
                )
                .await?;
            let fallback = rows.first().and_then(|v| v["id"].as_str());
            let ids = rows
                .iter()
                .filter_map(|v| v["id"].as_str().map(str::to_owned))
                .collect();
            accounts::validate_retry(&r, &j, &ids, fallback)?;
        }
        Ok(j)
    }
    pub async fn checkpoint(&self, progress: Option<f64>) -> TaskResult<()> {
        let mut tx = self.state.db.begin().await?;
        self.guard_tx(&mut tx).await?;
        if let Some(p) = progress {
            tx.execute("UPDATE jobs SET progress=?,lease_until=?,updated_at=? WHERE id=? AND status='running' AND worker_id=?",&[json!(p.clamp(0.0,100.0)),json!(future_time(LEASE_SECONDS)),json!(now()),json!(self.id),json!(self.owner)]).await?;
        } else {
            tx.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE id=? AND status='running' AND worker_id=?",&[json!(future_time(LEASE_SECONDS)),json!(now()),json!(self.id),json!(self.owner)]).await?;
        }
        tx.commit().await?;
        Ok(())
    }
    pub async fn request_gate(&self) -> TaskResult<()> {
        loop {
            let mut tx = self.state.db.begin().await?;
            self.guard_tx(&mut tx).await?;
            let c = tx
                .query("SELECT * FROM queue_control WHERE id=1", &[])
                .await?
                .remove(0);
            let moment = chrono::Utc::now().naive_utc();
            let pacing = if truth(&c["rate_limit_enabled"]) {
                date(&c["next_request_at"])
                    .map(|t| (t - moment).num_milliseconds() as f64 / 1000.0)
                    .unwrap_or(0.0)
            } else {
                0.0
            };
            let cooldown = date(&c["backoff_until"])
                .map(|t| (t - moment).num_milliseconds() as f64 / 1000.0)
                .unwrap_or(0.0);
            let remaining = pacing.max(cooldown).max(0.0);
            if remaining <= 0.0 {
                let next = if truth(&c["rate_limit_enabled"]) {
                    let min = c["min_interval_seconds"].as_f64().unwrap_or(2.0);
                    let max = c["max_interval_seconds"].as_f64().unwrap_or(5.0);
                    if !min.is_finite() || !max.is_finite() || min < 0.0 || min > max || max > 60.0
                    {
                        return Err(ApiError::bad("请求间隔配置无效，请重新保存").into());
                    }
                    let seconds = { rand::thread_rng().gen_range(min..=max) };
                    json!(
                        (moment + chrono::Duration::milliseconds((seconds * 1000.0) as i64))
                            .format("%Y-%m-%d %H:%M:%S%.6f")
                            .to_string()
                    )
                } else {
                    Value::Null
                };
                tx.execute(
                    "UPDATE queue_control SET next_request_at=? WHERE id=1",
                    &[next],
                )
                .await?;
                tx.commit().await?;
                return Ok(());
            }
            drop(tx);
            tokio::select! {_=self.stop.cancelled()=>return Err(TaskError::Paused),_=tokio::time::sleep(Duration::from_secs_f64(remaining.min(0.25)))=>()}
        }
    }
    pub async fn defer(&self, retry_after: Option<&str>) -> TaskResult<()> {
        let delay = retry_after
            .and_then(|s| {
                s.parse::<f64>().ok().or_else(|| {
                    httpdate::parse_http_date(s).ok().map(|t| {
                        t.duration_since(std::time::SystemTime::now())
                            .map(|d| d.as_secs_f64())
                            .unwrap_or(0.0)
                    })
                })
            })
            .filter(|f| f.is_finite() && *f >= 0.0)
            .unwrap_or(60.0)
            .min(3600.0);
        let until = chrono::Utc::now().naive_utc()
            + chrono::Duration::milliseconds((delay * 1000.0) as i64);
        let mut tx = self.state.db.begin().await?;
        let c = queue::locked(&self.state, &mut tx).await?;
        let backoff = date(&c["backoff_until"])
            .map(|t| t.max(until))
            .unwrap_or(until);
        let next = date(&c["next_request_at"])
            .map(|t| t.max(until))
            .unwrap_or(until);
        tx.execute(
            "UPDATE queue_control SET backoff_until=?,next_request_at=? WHERE id=1",
            &[
                json!(backoff.format("%Y-%m-%d %H:%M:%S%.6f").to_string()),
                json!(next.format("%Y-%m-%d %H:%M:%S%.6f").to_string()),
            ],
        )
        .await?;
        tx.commit().await?;
        Ok(())
    }
}
pub trait Executor: Send + Sync + 'static {
    fn execute(
        &self,
        ctx: Context,
        job: Value,
    ) -> Pin<Box<dyn Future<Output = TaskResult<Value>> + Send + 'static>>;
}
pub async fn execute<E: Executor + ?Sized>(
    s: AppState,
    c: Claim,
    executor: Arc<E>,
    stop: CancellationToken,
) -> Result<()> {
    let ctx = Context::new(s.clone(), &c, stop.clone());
    let heartbeat = async {
        loop {
            tokio::time::sleep(Duration::from_secs(5)).await;
            ctx.checkpoint(None).await?;
        }
    };
    let result = match ctx.checkpoint(Some(0.0)).await {
        Ok(()) => {
            tokio::select! {r=executor.execute(ctx.clone(),c.job.clone())=>r,r=heartbeat=>r,_=stop.cancelled()=>Err(TaskError::Paused)}
        }
        Err(e) => Err(e),
    };
    let result = if stop.is_cancelled() {
        Err(TaskError::Paused)
    } else {
        result
    };
    let mut tx = s.db.begin().await?;
    let target = c.job["id"].as_str().unwrap();
    queue::locked(&s, &mut tx).await?;
    let result = if result.is_ok() {
        match ctx.guard_tx(&mut tx).await {
            Ok(_) => result,
            Err(e) => Err(e),
        }
    } else {
        result
    };
    match result {
        Ok(v) => {
            tx.execute("UPDATE jobs SET status='completed',progress=100,result=?,error=NULL,worker_id=NULL,lease_until=NULL,updated_at=? WHERE id=? AND status='running' AND worker_id=?",&[json!(v.to_string()),json!(now()),json!(target),json!(c.owner)]).await?;
        }
        Err(TaskError::Paused | TaskError::Cancelled) => {
            tx.execute("UPDATE jobs SET status='queued',worker_id=NULL,lease_until=NULL,updated_at=? WHERE id=? AND status='running' AND worker_id=?",&[json!(now()),json!(target),json!(c.owner)]).await?;
        }
        Err(TaskError::Failed(e)) => {
            let changed=tx.execute("UPDATE jobs SET status='failed',error=?,worker_id=NULL,lease_until=NULL,updated_at=? WHERE id=? AND status='running' AND worker_id=?",&[json!(e.1),json!(now()),json!(target),json!(c.owner)]).await?;
            let p = json_column(&c.job["payload"]);
            if changed > 0
                && let Some(id) = p["subscription_id"].as_str()
            {
                tx.execute("UPDATE subscriptions SET last_error=?,last_checked_at=?,updated_at=? WHERE id=?",&[json!(e.1),json!(now()),json!(now()),json!(id)]).await?;
            }
            if changed > 0 {
                if let Some(entry) = p["watchlist_entry_id"].as_str() {
                    tx.execute("UPDATE pixiv_watchlist SET last_error=?,last_checked_at=?,updated_at=? WHERE id=? AND account_id=?",&[json!(e.1),json!(now()),json!(now()),json!(entry),p["account_id"].clone()]).await?;
                }
                if p["watchlist"] == true {
                    let mut state =
                        crate::executor::setting_tx(&s, &mut tx, crate::watchlist::KEY).await?;
                    if state["account_id"] == p["account_id"] {
                        state["last_error"] = json!(e.1);
                        state["last_checked_at"] = json!(now());
                        secrets::put_tx(&s, &mut tx, crate::watchlist::KEY, &state).await?;
                    }
                }
            }
        }
    }
    sync_state(&mut tx, target).await?;
    tx.commit().await?;
    Ok(())
}
pub async fn enqueue_tx(_s: &AppState, tx: &mut Tx, kind: &str, payload: &Value) -> Result<Value> {
    if !payload.is_object() {
        return Err(ApiError::bad("任务参数必须是对象"));
    }
    let id = uuid::Uuid::new_v4().to_string();
    let t = now();
    tx.execute("INSERT INTO jobs(id,kind,status,progress,payload,result,error,attempts,created_at,updated_at,lease_until,worker_id) VALUES(?,?,'queued',0,?,NULL,NULL,0,?,?,NULL,NULL)",&[json!(id),json!(kind),json!(payload.to_string()),json!(t),json!(t)]).await?;
    Ok(tx
        .query("SELECT * FROM jobs WHERE id=?", &[json!(id)])
        .await?
        .remove(0))
}
pub async fn enqueue_import_tx(s: &AppState, tx: &mut Tx, mut p: Value) -> Result<Value> {
    let platform = p["platform"]
        .as_str()
        .ok_or_else(|| ApiError::bad("导入来源缺失"))?
        .to_owned();
    let remote = p["remote_id"]
        .as_str()
        .ok_or_else(|| ApiError::bad("作品 ID 缺失"))?
        .to_owned();
    let candidates=tx.query(&format!("SELECT * FROM jobs WHERE kind IN ('import','import_content') AND status IN ('queued','running') AND {}=? AND {}=? ORDER BY created_at,id",json_text(s,"payload","platform"),json_text(s,"payload","remote_id")),&[json!(platform),json!(remote)]).await?;
    let body = p["download"] != false;
    let mut selected = None;
    for mut j in candidates {
        let mut old = json_column(&j["payload"]);
        if j["kind"] == "import_content" || old["download"] != false || !body {
            j["payload"] = old;
            selected = Some(j);
            break;
        }
        if j["status"] == "queued" {
            old["download"] = json!(true);
            if !p["account_id"].is_null() {
                old["account_id"] = p["account_id"].clone();
            }
            j["payload"] = old;
            selected = Some(j);
            break;
        }
    }
    let item = tx
        .query(
            "SELECT id,content_job_id FROM items WHERE source=? AND source_id=?",
            &[json!(platform), json!(remote)],
        )
        .await?
        .pop();
    if let Some(i) = &item
        && p["item_id"].is_null()
    {
        p["item_id"] = i["id"].clone();
    }
    let mut job = if let Some(j) = selected {
        j
    } else {
        enqueue_tx(s, tx, "import", &p).await?
    };
    let mut payload = json_column(&job["payload"]);
    if p["pixiv_bookmark_state"].is_object() {
        payload["pixiv_bookmark_state"] = p["pixiv_bookmark_state"].clone();
    }
    if p["download_origin"] == "manual"
        && body
        && (job["status"] == "queued" || job["kind"] == "import" && platform != "pixiv")
    {
        payload["download_origin"] = json!("manual");
        if job["status"] == "queued" {
            for k in [
                "download_account_id",
                "details_account_id",
                "execution_account_id",
            ] {
                payload.as_object_mut().unwrap().remove(k);
            }
        }
    }
    if let Some(item) = item {
        if payload["item_id"].is_null() {
            payload["item_id"] = item["id"].clone();
        }
        if body {
            tx.execute("UPDATE items SET content_state=?,content_error=NULL,content_job_id=?,updated_at=? WHERE id=? AND NOT EXISTS(SELECT 1 FROM assets WHERE assets.item_id=items.id)",&[json!(if job["status"]=="running"&&(job["kind"]=="import_content"||platform=="pixiv"){"downloading"}else{"queued"}),job["id"].clone(),json!(now()),item["id"].clone()]).await?;
        }
    }
    tx.execute(
        "UPDATE jobs SET payload=?,updated_at=? WHERE id=?",
        &[json!(payload.to_string()), json!(now()), job["id"].clone()],
    )
    .await?;
    job["payload"] = payload;
    Ok(job)
}
