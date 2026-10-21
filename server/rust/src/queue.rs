use crate::{
    ApiJson, AppState,
    db::{Tx, iso, json_column, now, truth},
    error::{ApiError, Result},
    library::{Params, text},
    tasks,
};
use axum::{
    Json,
    extract::{Path, Query, State},
};
use serde::Deserialize;
use serde_json::{Value, json};
pub const LANES: [(&str, &str); 8] = [
    ("manual_download", "手动下载"),
    ("retry", "失败重试"),
    ("download", "自动下载"),
    ("details", "详情解析"),
    ("checks", "作者与订阅检查"),
    ("ai", "AI 识别"),
    ("translation", "AI 汉化"),
    ("other", "其他任务"),
];
pub const SOURCES: [(&str, &str); 5] = [
    ("pixiv", "Pixiv"),
    ("ehentai", "E-Hentai"),
    ("picacg", "哔咔 PicACG"),
    ("jm", "JM 天堂"),
    ("kemono", "Kemono"),
];
pub(crate) fn source_enabled(control: &Value, source: &str) -> bool {
    control["lanes"]["_sources"][source]["enabled"] != false
}
pub(crate) fn job_source(job: &Value) -> String {
    if matches!(
        job["kind"].as_str(),
        Some("author_profile" | "author_feed" | "series_sync" | "pixiv_bookmark")
    ) {
        "pixiv".to_owned()
    } else {
        json_column(&job["payload"])["platform"]
            .as_str()
            .unwrap_or("")
            .to_owned()
    }
}
pub(crate) fn source_expression(s: &AppState) -> String {
    let expression = format!(
        "CASE WHEN kind IN ('author_profile','author_feed','series_sync','pixiv_bookmark') THEN 'pixiv' ELSE COALESCE({},'') END",
        tasks::json_text(s, "payload", "platform")
    );
    if s.db.mysql() {
        // CASE over JSON_UNQUOTE can expose BLOB/BINARY metadata. Platform IDs
        // are short ASCII enums; explicitly return text instead of weakening
        // the shared database decoder for arbitrary binary columns.
        format!("CAST(({expression}) AS CHAR(32) CHARACTER SET utf8mb4)")
    } else {
        expression
    }
}
pub(crate) fn source_filter(control: &Value, expression: &str) -> (String, Vec<Value>) {
    let disabled: Vec<_> = SOURCES
        .iter()
        .filter(|(id, _)| !source_enabled(control, id))
        .map(|(id, _)| json!(id))
        .collect();
    if disabled.is_empty() {
        ("1=1".to_owned(), disabled)
    } else {
        (
            format!(
                "({expression}) NOT IN ({})",
                crate::library::placeholders(disabled.len())
            ),
            disabled,
        )
    }
}
async fn source_counts(s: &AppState) -> Result<Vec<Value>> {
    let expression = source_expression(s);
    s.db.query(&format!("SELECT queue_lane,{expression} AS source,status,COUNT(*) AS n FROM jobs WHERE queue_lane IN ('download','details','checks') AND status IN ('queued','running') GROUP BY queue_lane,{expression},status"), &[]).await
}
fn source_view(control: &Value, id: &str, label: &str, counts: &[Value]) -> Value {
    let enabled = source_enabled(control, id);
    let master = truth(&control["paused"]);
    let mut tally = json!({"queued":0,"running":0});
    for row in counts.iter().filter(|c| c["source"] == id) {
        if let Some(status) = row["status"]
            .as_str()
            .filter(|v| matches!(*v, "queued" | "running"))
        {
            tally[status] =
                json!(tally[status].as_i64().unwrap_or(0) + row["n"].as_i64().unwrap_or(0));
        }
    }
    json!({"id":id,"label":label,"enabled":enabled,"effective_enabled":enabled&&!master,"blocked_by_master":master,"counts":tally,"changed_at":control["lanes"]["_sources"][id]["changed_at"]})
}
pub async fn sources(State(s): State<AppState>) -> Result<Json<Value>> {
    let row = control(&s).await?;
    let counts = source_counts(&s).await?;
    Ok(Json(
        json!({"items":SOURCES.iter().map(|(id,label)|source_view(&row,id,label,&counts)).collect::<Vec<_>>()}),
    ))
}
pub async fn source(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    let (_, label) = SOURCES
        .iter()
        .find(|(key, _)| *key == id)
        .ok_or_else(|| ApiError::missing("采集来源不存在"))?;
    Ok(Json(source_view(
        &control(&s).await?,
        &id,
        label,
        &source_counts(&s).await?,
    )))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceChanges {
    enabled: bool,
}
pub async fn update_source(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(c): ApiJson<SourceChanges>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let (_, label) = SOURCES
        .iter()
        .find(|(key, _)| *key == id)
        .ok_or_else(|| ApiError::missing("采集来源不存在"))?;
    let mut tx = s.db.begin().await?;
    let mut row = locked(&s, &mut tx).await?;
    if c.enabled != source_enabled(&row, &id) {
        row["lanes"]["_sources"][&id] =
            json!({"enabled":c.enabled,"changed_at":iso(&json!(now()))});
        tx.execute(
            "UPDATE queue_control SET lanes=?,updated_at=? WHERE id=1",
            &[json!(row["lanes"].to_string()), json!(now())],
        )
        .await?;
    }
    tx.commit().await?;
    Ok(Json(source_view(
        &row,
        &id,
        label,
        &source_counts(&s).await?,
    )))
}
pub(crate) fn collection(l: &str) -> bool {
    matches!(l, "download" | "details" | "checks")
}
fn defaults() -> Value {
    json!({"paused":false,"rate_limit_enabled":true,"concurrency":1,"min_interval_seconds":2.0,"max_interval_seconds":5.0,"lanes":{}})
}
async fn control(s: &AppState) -> Result<Value> {
    let mut row =
        s.db.one("SELECT * FROM queue_control WHERE id=1", &[])
            .await?
            .unwrap_or_else(defaults);
    row["lanes"] = json_column(&row["lanes"]);
    if row["lanes"].is_null() {
        row["lanes"] = json!({});
    }
    Ok(row)
}
fn view(v: &Value) -> Value {
    json!({"paused":truth(&v["paused"]),"rate_limit_enabled":truth(&v["rate_limit_enabled"]),"concurrency":v["concurrency"],"min_interval_seconds":v["min_interval_seconds"],"max_interval_seconds":v["max_interval_seconds"]})
}
pub(crate) fn lane(v: &Value, key: &str) -> Value {
    json!({"paused":truth(&v["lanes"][key]["paused"]),"concurrency":v["lanes"][key]["concurrency"].as_i64().unwrap_or(if collection(key){v["concurrency"].as_i64().unwrap_or(1)}else{1})})
}
pub(crate) async fn locked(s: &AppState, tx: &mut Tx) -> Result<Value> {
    let sql = format!(
        "INSERT INTO queue_control(id,paused,rate_limit_enabled,concurrency,download_account_cursor,min_interval_seconds,max_interval_seconds,cover_streak,created_at,updated_at) VALUES(1,0,1,1,0,2,5,0,?,?) {}",
        s.db.upsert("id", "id")
    );
    tx.execute(&sql, &[json!(now()), json!(now())]).await?;
    let mut row = tx
        .query(
            &format!(
                "SELECT * FROM queue_control WHERE id=1{}",
                if s.db.mysql() { " FOR UPDATE" } else { "" }
            ),
            &[],
        )
        .await?
        .remove(0);
    row["lanes"] = json_column(&row["lanes"]);
    if row["lanes"].is_null() {
        row["lanes"] = json!({});
    }
    Ok(row)
}
pub async fn settings(State(s): State<AppState>) -> Result<Json<Value>> {
    Ok(Json(view(&control(&s).await?)))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Changes {
    paused: Option<bool>,
    rate_limit_enabled: Option<bool>,
    concurrency: Option<u8>,
    min_interval_seconds: Option<f64>,
    max_interval_seconds: Option<f64>,
    request_interval_seconds: Option<f64>,
    pause_reason: Option<String>,
}
fn validate(c: &Changes) -> Result<()> {
    if c.concurrency.is_some_and(|v| !(1..=8).contains(&v)) {
        return Err(ApiError::invalid("并发任务数必须是 1–8 的整数"));
    }
    if c.pause_reason
        .as_deref()
        .is_some_and(|v| !matches!(v, "manual" | "maintenance") || c.paused != Some(true))
    {
        return Err(ApiError::bad("暂停原因只能在暂停时指定"));
    }
    if c.request_interval_seconds.is_some()
        && (c.min_interval_seconds.is_some() || c.max_interval_seconds.is_some())
    {
        return Err(ApiError::bad("固定间隔与随机间隔不能同时指定"));
    }
    Ok(())
}
fn event(c: &Changes) -> Value {
    json!({"paused":c.paused.unwrap_or(false),"source":c.pause_reason.as_deref().unwrap_or("manual"),"at":iso(&json!(now()))})
}
pub async fn update_settings(
    State(s): State<AppState>,
    ApiJson(c): ApiJson<Changes>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    validate(&c)?;
    let mut tx = s.db.begin().await?;
    let mut row = locked(&s, &mut tx).await?;
    if let Some(v) = c.paused {
        if v != truth(&row["paused"]) {
            row["lanes"]["_collection_pause"] = event(&c);
        }
        row["paused"] = json!(v);
    }
    if let Some(v) = c.rate_limit_enabled {
        row["rate_limit_enabled"] = json!(v);
        if !v {
            row["next_request_at"] = Value::Null;
        }
    }
    if let Some(v) = c.concurrency {
        row["concurrency"] = json!(v);
        for (k, _) in LANES {
            if collection(k) {
                let mut record = row["lanes"][k].clone();
                if !record.is_object() {
                    record = lane(&row, k);
                }
                record["concurrency"] = json!(v);
                row["lanes"][k] = record;
            }
        }
    }
    if let Some(v) = c.request_interval_seconds.or(c.min_interval_seconds) {
        row["min_interval_seconds"] = json!(v);
    }
    if let Some(v) = c.request_interval_seconds.or(c.max_interval_seconds) {
        row["max_interval_seconds"] = json!(v);
    }
    let min = row["min_interval_seconds"].as_f64().unwrap_or(f64::NAN);
    let max = row["max_interval_seconds"].as_f64().unwrap_or(f64::NAN);
    if !min.is_finite() || !max.is_finite() || min < 0.0 || min > max || max > 60.0 {
        return Err(ApiError::bad("请求间隔必须满足 0 ≤ 最短 ≤ 最长 ≤ 60 秒"));
    }
    // Preserve outstanding upstream cooldowns and lane-specific limits.
    tx.execute("UPDATE queue_control SET paused=?,rate_limit_enabled=?,concurrency=?,min_interval_seconds=?,max_interval_seconds=?,lanes=?,next_request_at=?,updated_at=? WHERE id=1",&[json!(truth(&row["paused"])),json!(truth(&row["rate_limit_enabled"])),row["concurrency"].clone(),json!(min),json!(max),json!(row["lanes"].to_string()),sql_datetime(&row["next_request_at"]),json!(now())]).await?;
    tx.commit().await?;
    Ok(Json(view(&row)))
}
fn sql_datetime(v: &Value) -> Value {
    v.as_str()
        .map(|s| json!(s.trim_end_matches('Z').replace('T', " ")))
        .unwrap_or(Value::Null)
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LaneChanges {
    paused: Option<bool>,
    concurrency: Option<u8>,
    pause_reason: Option<String>,
}
pub async fn update_lane(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(c): ApiJson<LaneChanges>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    if !LANES.iter().any(|(k, _)| *k == id) {
        return Err(ApiError::missing("队列不存在"));
    }
    let changes = Changes {
        paused: c.paused,
        concurrency: c.concurrency,
        pause_reason: c.pause_reason,
        rate_limit_enabled: None,
        min_interval_seconds: None,
        max_interval_seconds: None,
        request_interval_seconds: None,
    };
    validate(&changes)?;
    let mut tx = s.db.begin().await?;
    let mut row = locked(&s, &mut tx).await?;
    let current = lane(&row, &id);
    let mut record = row["lanes"][&id].clone();
    if !record.is_object() {
        record = current.clone();
    }
    if let Some(p) = changes.paused {
        if p != truth(&current["paused"]) {
            record["pause_event"] = event(&changes);
        }
        record["paused"] = json!(p);
    }
    if let Some(n) = changes.concurrency {
        record["concurrency"] = json!(n);
    }
    row["lanes"][&id] = record;
    tx.execute(
        "UPDATE queue_control SET lanes=?,updated_at=? WHERE id=1",
        &[json!(row["lanes"].to_string()), json!(now())],
    )
    .await?;
    tx.commit().await?;
    Ok(Json(lane(&row, &id)))
}
pub(crate) fn original_lane(kind: &str, p: &Value) -> &'static str {
    if matches!(kind, "import" | "import_content") && p["download_origin"] == "manual" {
        "manual_download"
    } else if kind == "import_content"
        || (kind == "import" && p["platform"] == "pixiv" && p["download"] != false)
    {
        "download"
    } else {
        match kind {
            "import" => "details",
            "source_sync" | "series_sync" | "author_profile" | "author_feed" | "pixiv_bookmark" => {
                "checks"
            }
            "ai" => "ai",
            "translate" => "translation",
            _ => "other",
        }
    }
}
pub fn job_view(row: Value) -> Value {
    let p = json_column(&row["payload"]);
    let result = json_column(&row["result"]);
    let kind = row["kind"].as_str().unwrap_or("");
    let original = original_lane(kind, &p);
    let label = if truth(&p["watchlist"]) {
        Some("Pixiv 追更列表同步")
    } else if kind == "pixiv_bookmark" {
        Some("Pixiv 主账号收藏检查")
    } else if kind == "import"
        && p["platform"] == "pixiv"
        && p["download"] != false
        && !(row["status"] == "completed" && result["metadata_only"] == true)
    {
        Some("Pixiv 详情与正文下载")
    } else {
        None
    };
    json!({"id":row["id"],"kind":row["kind"],"queue":if p["retry_queue"]==true{"retry"}else{original},"original_queue":original,"status":row["status"],"progress":row["progress"],"error":row["error"],"retry_last_error":p["retry_last_error"],"created_at":iso(&row["created_at"]),"updated_at":iso(&row["updated_at"]),"result":result,"item_id":p.get("item_id").filter(|v|!v.is_null()).unwrap_or(&result["item_id"]),"label":label})
}
pub async fn jobs(State(s): State<AppState>, Query(q): Query<Params>) -> Result<Json<Value>> {
    let lane = text(&q, "queue");
    if !lane.is_empty() && !LANES.iter().any(|(k, _)| *k == lane) {
        return Err(ApiError::invalid("队列不存在"));
    }
    let mut sql = "SELECT * FROM jobs".to_owned();
    let mut args = vec![];
    if !lane.is_empty() {
        sql.push_str(" WHERE queue_lane=?");
        args.push(json!(lane));
    }
    sql.push_str(" ORDER BY created_at DESC,id LIMIT 200");
    Ok(Json(
        json!({"items":s.db.query(&sql,&args).await?.into_iter().map(job_view).collect::<Vec<_>>()}),
    ))
}
pub async fn job(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    let row =
        s.db.one("SELECT * FROM jobs WHERE id=?", &[json!(id)])
            .await?
            .ok_or_else(|| ApiError::missing("任务不存在"))?;
    Ok(Json(job_view(row)))
}
pub async fn status(State(s): State<AppState>) -> Result<Json<Value>> {
    let row = control(&s).await?;
    let source_counts = source_counts(&s).await?;
    let proxy_ready = crate::network::managed_ready(&s).await.unwrap_or(false);
    let counts =
        s.db.query(
            "SELECT queue_lane,status,COUNT(*) AS n FROM jobs GROUP BY queue_lane,status",
            &[],
        )
        .await?;
    let mut lanes = vec![];
    for (id, label) in LANES {
        let mut entry = lane(&row, id);
        entry["id"] = json!(id);
        entry["label"] = json!(label);
        entry["blocked_by_master"] = json!(truth(&row["paused"]) && collection(id));
        let mut tally = json!({"queued":0,"running":0,"failed":0,"completed":0});
        for c in counts.iter().filter(|c| c["queue_lane"] == id) {
            if let Some(key) = c["status"]
                .as_str()
                .filter(|s| matches!(*s, "queued" | "running" | "failed" | "completed"))
            {
                tally[key] = c["n"].clone();
            }
        }
        entry["counts"] = tally;
        let blocked_sources: Vec<_> = SOURCES
            .iter()
            .filter(|(source, _)| !source_enabled(&row, source))
            .filter_map(|(source, label)| {
                let queued: i64 = source_counts
                    .iter()
                    .filter(|c| {
                        c["queue_lane"] == id && c["source"] == *source && c["status"] == "queued"
                    })
                    .map(|c| c["n"].as_i64().unwrap_or(0))
                    .sum();
                (queued > 0).then(|| json!({"id":source,"label":label,"queued":queued}))
            })
            .collect();
        let blocked_count: i64 = blocked_sources
            .iter()
            .map(|c| c["queued"].as_i64().unwrap_or(0))
            .sum();
        entry["blocked_sources"] = json!(blocked_sources);
        let runtime = s.runtime.read().map(|v| v.clone()).unwrap_or(Value::Null);
        let worker = runtime["workers"]
            .as_array()
            .and_then(|v| v.iter().find(|v| v["lane"] == id));
        let paused = truth(&entry["paused"]) || truth(&entry["blocked_by_master"]);
        let running = worker.is_some_and(|v| v["running"] == true);
        let (state, code, message, action) = if paused {
            (
                "paused",
                "paused",
                "此队列或统一采集开关已暂停",
                "检查队列独立开关及暂停来源",
            )
        } else if blocked_count > 0
            && blocked_count == entry["counts"]["queued"].as_i64().unwrap_or(0)
            && entry["counts"]["running"] == 0
        {
            (
                "waiting",
                "source_paused",
                "等待任务所属来源的采集开关已关闭",
                "在来源采集中开启对应平台；手动下载和失败重试独立运行",
            )
        } else if !running {
            (
                "waiting",
                "worker_unavailable",
                "队列进程未启动或正在重启",
                "检查 Rust 进程设置及数据库连接",
            )
        } else if !proxy_ready
            && matches!(
                id,
                "download" | "details" | "checks" | "manual_download" | "retry"
            )
            && entry["counts"]["queued"].as_i64().unwrap_or(0) > 0
            && entry["counts"]["running"].as_i64().unwrap_or(0) == 0
        {
            (
                "waiting",
                "proxy_unavailable",
                "内置代理正在启动或恢复，平台任务等待代理就绪",
                "检查内置代理状态与选中节点；独立 AI 等任务继续按各自配置执行",
            )
        } else if tasks::date(&row["backoff_until"])
            .is_some_and(|d| d > chrono::Utc::now().naive_utc())
        {
            (
                "waiting",
                "rate_limit_cooldown",
                "平台请求冷却尚未结束",
                "等待平台冷却结束，不会因关闭限速而跳过",
            )
        } else if entry["counts"]["running"].as_i64().unwrap_or(0) > 0 {
            ("running", "running", "任务正在执行", "")
        } else if entry["counts"]["queued"].as_i64().unwrap_or(0) > 0 {
            (
                "waiting",
                "pending_dispatch",
                "等待执行账号或队列并发名额",
                "检查账号功能与重试权限、账号并发设置",
            )
        } else {
            ("idle", "idle", "当前队列没有等待任务", "")
        };
        entry["diagnostic"] = json!({"state":state,"code":code,"reasons":[{"code":code,"message":message,"action":action}]});
        if paused {
            let mut reasons = vec![];
            if truth(&entry["paused"]) {
                let event = &row["lanes"][id]["pause_event"];
                reasons.push(json!({"code":"lane_paused","message":if event["source"]=="maintenance"{"此队列因维护暂停"}else{"此队列已手动暂停"},"action":"打开此队列的独立开关后恢复执行","source":event["source"],"at":event["at"]}));
            }
            if truth(&entry["blocked_by_master"]) {
                let event = &row["lanes"]["_collection_pause"];
                reasons.push(json!({"code":"collection_paused","message":if event["source"]=="maintenance"{"统一采集因维护暂停"}else{"统一采集开关已关闭"},"action":"打开统一采集开关后恢复自动采集；手动与重试队列独立控制","source":event["source"],"at":event["at"]}));
            }
            entry["diagnostic"]["reasons"] = json!(reasons);
        }
        lanes.push(entry);
    }
    let mut totals = json!({"queued":0,"running":0,"failed":0});
    for c in s.db.query("SELECT status,COUNT(*) AS n FROM jobs WHERE kind IN ('import','import_content','source_sync','series_sync','author_profile','author_feed','pixiv_bookmark') GROUP BY status",&[]).await?{if let Some(k)=c["status"].as_str().filter(|s|matches!(*s,"queued"|"running"|"failed")){totals[k]=c["n"].clone();}}
    let active=s.db.query("SELECT * FROM jobs WHERE status='running' AND kind IN ('import','import_content','source_sync','series_sync','author_profile','author_feed','pixiv_bookmark') ORDER BY created_at,id",&[]).await?.into_iter().map(job_view).collect::<Vec<_>>();
    Ok(Json(
        json!({"settings":view(&row),"sources":SOURCES.iter().map(|(id,label)|source_view(&row,id,label,&source_counts)).collect::<Vec<_>>(),"counts":totals,"active":active.first(),"active_jobs":active,"lanes":lanes,"runtime":s.runtime.read().map(|v|v.clone()).unwrap_or(Value::Null)}),
    ))
}
