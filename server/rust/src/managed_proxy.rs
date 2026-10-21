//! Owned Mihomo child lifecycle and bounded controller access. No system proxy
//! changes, remote rules or transparent direct fallback are permitted.
use crate::{
    ApiJson, AppState,
    error::{ApiError, Result},
    network, proxy_subscription, secrets,
};
use axum::{
    Json,
    extract::{Path, State},
    http::StatusCode,
};
use futures_util::{StreamExt, stream};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    path::PathBuf,
    process::Stdio,
    time::{Duration, Instant},
};
use tokio::io::AsyncWriteExt;
use tokio_util::sync::CancellationToken;
const KEY: &str = "managed_proxy";
const VERSION: &str = "v1.19.32";
fn unavailable() -> ApiError {
    ApiError(
        StatusCode::SERVICE_UNAVAILABLE,
        "内置代理无法应用配置，请检查内核状态；失败不会回退直连",
    )
}
fn port(name: &str, default: u16) -> Result<u16> {
    std::env::var(name)
        .unwrap_or_else(|_| default.to_string())
        .parse::<u16>()
        .ok()
        .filter(|n| *n >= 1024)
        .ok_or_else(unavailable)
}
fn binary() -> PathBuf {
    std::env::var_os("LIBRARY_MIHOMO_PATH")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(if cfg!(windows) {
                ".runtime/mihomo/mihomo.exe"
            } else {
                ".runtime/mihomo/mihomo"
            })
        })
}
fn nodes(v: &Value) -> impl Iterator<Item = &Value> {
    v["subscriptions"]
        .as_array()
        .into_iter()
        .flatten()
        .flat_map(|s| s["nodes"].as_array().into_iter().flatten())
}
pub async fn read(s: &AppState) -> Result<Value> {
    let v = secrets::get(s, KEY)
        .await?
        .unwrap_or(json!({"enabled":false,"subscriptions":[],"selected_node_id":null}));
    if !v["subscriptions"].is_array()
        || v["subscriptions"].as_array().is_some_and(|a| a.len() > 10)
        || nodes(&v).count() > 2000
    {
        return Err(unavailable());
    }
    Ok(v)
}
fn stamp() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}
fn schedule(hours: u64) -> String {
    (chrono::Utc::now() + chrono::Duration::hours(hours as i64))
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}
fn profile(v: &Value, secret: &str) -> Result<Value> {
    let proxy = port("LIBRARY_MIHOMO_PROXY_PORT", 17890)?;
    let controller = port("LIBRARY_MIHOMO_CONTROLLER_PORT", 19090)?;
    if proxy == controller {
        return Err(unavailable());
    }
    let mut names = vec![json!("REJECT")];
    let mut proxies = vec![];
    for node in nodes(v) {
        let mut config = proxy_subscription::sanitize(node["config"].clone())?;
        config["name"] = node["id"].clone();
        names.push(node["id"].clone());
        proxies.push(config);
    }
    let selected = if names.contains(&v["selected_node_id"]) {
        v["selected_node_id"].clone()
    } else {
        json!("REJECT")
    };
    Ok(
        json!({"mixed-port":proxy,"allow-lan":false,"bind-address":"127.0.0.1","mode":"rule","log-level":"silent","ipv6":false,"find-process-mode":"off","external-controller":format!("127.0.0.1:{controller}"),"secret":secret,"external-controller-cors":{"allow-origins":[],"allow-private-network":false},"profile":{"store-selected":false,"store-fake-ip":false},"geo-auto-update":false,"tun":{"enable":false},"dns":{"enable":false},"proxies":proxies,"proxy-groups":[{"name":"QIYE","type":"select","proxies":names,"default-selected":selected}],"rules":["MATCH,QIYE"]}),
    )
}
#[derive(Default)]
pub struct Core {
    child: Option<tokio::process::Child>,
    dir: Option<tempfile::TempDir>,
    secret: String,
    version: Option<String>,
    restart_count: u32,
    last_restart: Option<Instant>,
    error: Option<&'static str>,
    count: usize,
    selected: Value,
}
fn command() -> tokio::process::Command {
    let mut cmd = tokio::process::Command::new(binary());
    let env = std::env::vars_os()
        .filter(|(k, _)| {
            [
                "PATH",
                "SYSTEMROOT",
                "WINDIR",
                "TEMP",
                "TMP",
                "LANG",
                "LC_ALL",
                "TZ",
            ]
            .iter()
            .any(|s| k.eq_ignore_ascii_case(s))
        })
        .collect::<Vec<_>>();
    cmd.env_clear()
        .envs(env)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    #[cfg(windows)]
    cmd.creation_flags(0x08000000);
    cmd
}
impl Core {
    fn alive(&mut self) -> bool {
        self.child
            .as_mut()
            .is_some_and(|c| c.try_wait().is_ok_and(|v| v.is_none()))
    }
    fn status(&mut self) -> Value {
        let running = self.alive();
        json!({"installed":binary().is_file(),"running":running,"version":self.version,"state":if running{"running"}else if self.error.is_some(){"failed"}else{"stopped"},"error":self.error,"node_count":self.count,"selected":self.selected,"proxy_url":format!("http://127.0.0.1:{}",port("LIBRARY_MIHOMO_PROXY_PORT",17890).unwrap_or(17890)),"restart_count":self.restart_count})
    }
    async fn request(
        &self,
        method: reqwest::Method,
        path: &str,
        body: Option<Value>,
        timeout: Duration,
    ) -> Result<Value> {
        let client = network::direct_client(timeout)?;
        let mut req = client
            .request(
                method,
                format!(
                    "http://127.0.0.1:{}{path}",
                    port("LIBRARY_MIHOMO_CONTROLLER_PORT", 19090)?
                ),
            )
            .bearer_auth(&self.secret);
        if let Some(body) = body {
            req = req.json(&body);
        }
        let response = req.send().await.map_err(|_| unavailable())?;
        if !response.status().is_success() {
            return Err(unavailable());
        }
        let mut bytes = vec![];
        let mut stream = response.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| unavailable())?;
            if bytes.len() + chunk.len() > 2 * 1024 * 1024 {
                return Err(unavailable());
            }
            bytes.extend_from_slice(&chunk);
        }
        if bytes.is_empty() {
            Ok(json!({}))
        } else {
            serde_json::from_slice(&bytes).map_err(|_| unavailable())
        }
    }
    async fn stop(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.kill().await;
        }
        self.dir = None;
        self.version = None;
    }
    async fn apply(&mut self, v: &Value) -> Result<()> {
        if self.secret.is_empty() {
            self.secret = format!(
                "{}{}",
                uuid::Uuid::new_v4().simple(),
                uuid::Uuid::new_v4().simple()
            );
        }
        let p = profile(v, &self.secret)?;
        let dir = tempfile::Builder::new().prefix("qiye-mihomo-").tempdir()?;
        let path = dir.path().join("config.json");
        let mut opt = tokio::fs::OpenOptions::new();
        opt.write(true).create_new(true);
        #[cfg(unix)]
        opt.mode(0o600);
        let mut f = opt.open(&path).await?;
        f.write_all(p.to_string().as_bytes()).await?;
        f.sync_all().await?;
        drop(f);
        let status = tokio::time::timeout(
            Duration::from_secs(15),
            command()
                .arg("-t")
                .arg("-d")
                .arg(dir.path())
                .arg("-f")
                .arg(&path)
                .status(),
        )
        .await
        .map_err(|_| unavailable())?
        .map_err(|_| unavailable())?;
        if !status.success() {
            return Err(unavailable());
        }
        if self.alive() {
            self.request(
                reqwest::Method::PUT,
                "/configs?force=true",
                Some(json!({"payload":p.to_string()})),
                Duration::from_secs(10),
            )
            .await?;
        } else {
            self.stop().await;
            self.child = Some(
                command()
                    .arg("-d")
                    .arg(dir.path())
                    .arg("-f")
                    .arg(&path)
                    .spawn()
                    .map_err(|_| unavailable())?,
            );
            self.dir = Some(dir);
            let mut ready = false;
            for _ in 0..30 {
                if !self.alive() {
                    break;
                }
                if let Ok(data) = self
                    .request(
                        reqwest::Method::GET,
                        "/version",
                        None,
                        Duration::from_secs(1),
                    )
                    .await
                {
                    let version = data["version"].as_str().unwrap_or("");
                    if version.trim_start_matches('v') != VERSION.trim_start_matches('v') {
                        self.stop().await;
                        return Err(unavailable());
                    }
                    self.version = Some(version.into());
                    ready = true;
                    break;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            if !ready {
                self.stop().await;
                return Err(unavailable());
            }
        }
        self.count = nodes(v).count();
        self.selected = p["proxy-groups"][0]["default-selected"].clone();
        self.error = None;
        Ok(())
    }
}
fn publish(s: &AppState, c: &mut Core) {
    if let Ok(mut snapshot) = s.proxy_status.write() {
        *snapshot = c.status();
    }
}
async fn persist(s: &AppState, c: &mut Core, v: &Value) -> Result<()> {
    if v.to_string().len() > 8 * 1024 * 1024 || nodes(v).count() > 2000 {
        return Err(ApiError::bad("订阅总计超过 2000 个节点或配置超过 8 MiB"));
    }
    if (v["enabled"] == true || c.alive())
        && let Err(e) = c.apply(v).await
    {
        c.stop().await;
        c.error = Some("内核无法应用配置，原配置保留");
        publish(s, c);
        return Err(e);
    }
    if let Err(e) = secrets::put(s, KEY, v).await {
        c.stop().await;
        c.error = Some("配置保存失败，已停止内核");
        publish(s, c);
        return Err(e);
    }
    publish(s, c);
    Ok(())
}
pub async fn status(State(s): State<AppState>) -> Result<Json<Value>> {
    let v = read(&s).await?;
    let subscriptions=v["subscriptions"].as_array().unwrap().iter().map(|sub|{let mut out=json!({"url_host":sub["url"].as_str().and_then(|url|url::Url::parse(url).ok()).and_then(|u|u.host_str().map(str::to_owned)),"node_count":sub["nodes"].as_array().map(Vec::len).unwrap_or(0)});for k in["id","label","auto_update","interval_hours","fetch_via_proxy","last_updated_at","last_error","next_update_at"]{out[k]=sub[k].clone();}out}).collect::<Vec<_>>();
    let nodes=v["subscriptions"].as_array().unwrap().iter().flat_map(|sub|sub["nodes"].as_array().into_iter().flatten().map(|node|json!({"id":node["id"],"name":node["name"],"type":node["config"]["type"],"subscription_id":sub["id"],"subscription_label":sub["label"],"selected":node["id"]==v["selected_node_id"],"delay":node["delay"],"delay_checked_at":node["delay_checked_at"]}))).collect::<Vec<_>>();
    let core = s
        .proxy_status
        .read()
        .map(|v| v.clone())
        .unwrap_or(json!({"running":false,"state":"unavailable"}));
    Ok(Json(
        json!({"enabled":v["enabled"],"core":core,"subscriptions":subscriptions,"nodes":nodes,"selected_node_id":v["selected_node_id"],"message":"内置代理优先于高级外部代理；失败不会回退直连。仅代理书库服务端的新连接，不修改系统代理。"}),
    ))
}
#[derive(Deserialize)]
pub struct Subscription {
    label: String,
    url: String,
    #[serde(default = "yes")]
    auto_update: bool,
    #[serde(default = "hours")]
    interval_hours: u64,
    #[serde(default)]
    fetch_via_proxy: bool,
}
fn yes() -> bool {
    true
}
fn hours() -> u64 {
    24
}
#[derive(Deserialize)]
pub struct Patch {
    label: Option<String>,
    url: Option<String>,
    auto_update: Option<bool>,
    interval_hours: Option<u64>,
    fetch_via_proxy: Option<bool>,
}
fn validate(sub: &Value) -> Result<()> {
    let label = sub["label"].as_str().unwrap_or("");
    if label.trim().is_empty()
        || label.chars().count() > 100
        || label.chars().any(char::is_control)
        || !(1..=168).contains(&sub["interval_hours"].as_u64().unwrap_or(0))
    {
        return Err(ApiError::invalid("订阅名称或更新间隔无效"));
    }
    proxy_subscription::validate_url(sub["url"].as_str().unwrap_or(""))?;
    Ok(())
}
fn mapped(id: &str, definitions: Vec<Value>, old: &Value) -> Value {
    json!(definitions.into_iter().map(|config|{let name=config["name"].as_str().unwrap_or("");let digest=Sha256::digest(format!("{id}\0{name}").as_bytes());let key=format!("q-{}",digest[..16].iter().map(|b|format!("{b:02x}")).collect::<String>());let previous=old.as_array().into_iter().flatten().find(|node|node["id"]==key&&node["config"]==config);json!({"id":key,"name":name,"config":config,"delay":previous.map(|n|n["delay"].clone()),"delay_checked_at":previous.map(|n|n["delay_checked_at"].clone())})}).collect::<Vec<_>>())
}
pub async fn add(
    State(s): State<AppState>,
    ApiJson(input): ApiJson<Subscription>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut c = s.proxy_core.lock().await;
    let mut v = read(&s).await?;
    if v["subscriptions"].as_array().unwrap().len() >= 10 {
        return Err(ApiError::bad("最多保存 10 个订阅"));
    }
    let id = uuid::Uuid::new_v4().simple().to_string();
    let mut sub = json!({"id":id,"label":input.label.trim(),"url":input.url.trim(),"auto_update":input.auto_update,"interval_hours":input.interval_hours,"fetch_via_proxy":input.fetch_via_proxy});
    validate(&sub)?;
    if v["subscriptions"]
        .as_array()
        .unwrap()
        .iter()
        .any(|old| old["url"] == sub["url"])
    {
        return Err(ApiError::bad("订阅已存在"));
    }
    let definitions =
        proxy_subscription::fetch(&s, sub["url"].as_str().unwrap(), input.fetch_via_proxy).await?;
    sub["nodes"] = mapped(&id, definitions, &Value::Null);
    sub["last_updated_at"] = json!(stamp());
    sub["last_error"] = Value::Null;
    sub["next_update_at"] = json!(schedule(input.interval_hours));
    if v["selected_node_id"].is_null() && v["enabled"] != true {
        v["selected_node_id"] = sub["nodes"][0]["id"].clone();
    }
    v["subscriptions"].as_array_mut().unwrap().push(sub);
    persist(&s, &mut c, &v).await?;
    drop(c);
    status(State(s)).await
}
async fn modify(s: &AppState, id: &str, patch: Option<Patch>, refresh: bool) -> Result<()> {
    s.config.writable()?;
    let mut c = s.proxy_core.lock().await;
    let mut v = read(s).await?;
    let index = v["subscriptions"]
        .as_array()
        .unwrap()
        .iter()
        .position(|sub| sub["id"] == id)
        .ok_or(ApiError::missing("订阅不存在"))?;
    let old = v["subscriptions"][index].clone();
    let mut sub = old.clone();
    if let Some(p) = patch {
        if let Some(x) = p.label {
            sub["label"] = json!(x.trim());
        }
        if let Some(x) = p.url {
            sub["url"] = json!(x.trim());
        }
        if let Some(x) = p.auto_update {
            sub["auto_update"] = json!(x);
        }
        if let Some(x) = p.interval_hours {
            sub["interval_hours"] = json!(x);
        }
        if let Some(x) = p.fetch_via_proxy {
            sub["fetch_via_proxy"] = json!(x);
        }
    }
    validate(&sub)?;
    if v["subscriptions"]
        .as_array()
        .unwrap()
        .iter()
        .enumerate()
        .any(|(i, other)| i != index && other["url"] == sub["url"])
    {
        return Err(ApiError::bad("订阅已存在"));
    }
    if refresh || sub["url"] != old["url"] {
        match proxy_subscription::fetch(
            s,
            sub["url"].as_str().unwrap(),
            sub["fetch_via_proxy"] == true,
        )
        .await
        {
            Ok(defs) => {
                sub["nodes"] = mapped(id, defs, &old["nodes"]);
                sub["last_updated_at"] = json!(stamp());
                sub["last_error"] = Value::Null;
            }
            Err(e) => {
                v["subscriptions"][index]["last_error"] = json!("订阅更新失败，保留原节点");
                v["subscriptions"][index]["next_update_at"] =
                    json!(schedule(old["interval_hours"].as_u64().unwrap_or(24)));
                secrets::put(s, KEY, &v).await?;
                return Err(e);
            }
        }
    }
    sub["next_update_at"] = json!(schedule(sub["interval_hours"].as_u64().unwrap_or(24)));
    v["subscriptions"][index] = sub;
    if !nodes(&v).any(|n| n["id"] == v["selected_node_id"]) {
        v["selected_node_id"] = Value::Null;
    }
    persist(s, &mut c, &v).await
}
pub async fn patch(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(p): ApiJson<Patch>,
) -> Result<Json<Value>> {
    modify(&s, &id, Some(p), false).await?;
    status(State(s)).await
}
pub async fn refresh(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    modify(&s, &id, None, true).await?;
    status(State(s)).await
}
pub async fn remove(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut c = s.proxy_core.lock().await;
    let mut v = read(&s).await?;
    let subs = v["subscriptions"].as_array_mut().unwrap();
    let before = subs.len();
    subs.retain(|sub| sub["id"] != id);
    if subs.len() == before {
        return Err(ApiError::missing("订阅不存在"));
    }
    if !nodes(&v).any(|n| n["id"] == v["selected_node_id"]) {
        v["selected_node_id"] = Value::Null;
    }
    persist(&s, &mut c, &v).await?;
    drop(c);
    status(State(s)).await
}
#[derive(Deserialize)]
pub struct Enable {
    enabled: bool,
}
pub async fn enable(State(s): State<AppState>, ApiJson(p): ApiJson<Enable>) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut c = s.proxy_core.lock().await;
    let mut v = read(&s).await?;
    if p.enabled && !nodes(&v).any(|n| n["id"] == v["selected_node_id"]) {
        return Err(ApiError::bad("请先选择一个可用节点"));
    }
    v["enabled"] = json!(p.enabled);
    persist(&s, &mut c, &v).await?;
    if !p.enabled {
        c.stop().await;
        publish(&s, &mut c);
    }
    drop(c);
    status(State(s)).await
}
#[derive(Deserialize)]
pub struct Node {
    node_id: String,
}
pub async fn select(State(s): State<AppState>, ApiJson(p): ApiJson<Node>) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut c = s.proxy_core.lock().await;
    let mut v = read(&s).await?;
    if !nodes(&v).any(|n| n["id"] == p.node_id) {
        return Err(ApiError::missing("节点不存在"));
    }
    v["selected_node_id"] = json!(p.node_id);
    persist(&s, &mut c, &v).await?;
    drop(c);
    status(State(s)).await
}
pub async fn restart(State(s): State<AppState>) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut c = s.proxy_core.lock().await;
    let v = read(&s).await?;
    c.stop().await;
    let r = c.apply(&v).await;
    if r.is_err() {
        c.error = Some("内核启动失败，请检查已安装版本与端口");
    }
    publish(&s, &mut c);
    r?;
    drop(c);
    status(State(s)).await
}
async fn probe(s: &AppState, id: &str, save: bool) -> Result<(Value, String)> {
    let (secret, config) = {
        let mut c = s.proxy_core.lock().await;
        let v = read(s).await?;
        let node = nodes(&v)
            .find(|n| n["id"] == id)
            .ok_or(ApiError::missing("节点不存在"))?;
        let config = node["config"].clone();
        if !c.alive() {
            c.apply(&v).await?;
        }
        publish(s, &mut c);
        (c.secret.clone(), config)
    };
    let _permit = s
        .proxy_probe
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| unavailable())?;
    let safe = id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') && id.len() <= 128;
    if !safe {
        return Err(ApiError::bad("节点编号无效"));
    }
    let response = network::direct_client(Duration::from_secs(12))?
        .get(format!(
            "http://127.0.0.1:{}/proxies/{id}/delay",
            port("LIBRARY_MIHOMO_CONTROLLER_PORT", 19090)?
        ))
        .bearer_auth(secret)
        .query(&[
            ("url", "https://www.gstatic.com/generate_204"),
            ("timeout", "10000"),
        ])
        .send()
        .await;
    let delay = match response {
        Ok(r) if r.status().is_success() => network::bounded(r, 65536)
            .await
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            .and_then(|v| v["delay"].as_u64())
            .filter(|n| *n <= 60000),
        _ => None,
    };
    let signature = secrets::fingerprint(&config);
    let result = json!({"node_id":id,"delay":delay,"ok":delay.is_some(),"checked_at":stamp(),"message":if delay.is_some(){"测速完成"}else{"节点连接失败或超时"}});
    if save {
        save_delays(s, &[(id.into(), result.clone(), signature.clone())]).await?;
    }
    Ok((result, signature))
}
async fn save_delays(s: &AppState, results: &[(String, Value, String)]) -> Result<()> {
    let _core = s.proxy_core.lock().await;
    let mut v = read(s).await?;
    let lookup = results
        .iter()
        .map(|(id, result, signature)| (id.as_str(), (result, signature)))
        .collect::<std::collections::HashMap<_, _>>();
    let mut changed = false;
    for sub in v["subscriptions"].as_array_mut().unwrap() {
        for node in sub["nodes"].as_array_mut().unwrap() {
            if let Some((result, signature)) = lookup.get(node["id"].as_str().unwrap_or("")) {
                let checked = result["checked_at"].as_str().unwrap_or("");
                if &secrets::fingerprint(&node["config"]) == *signature
                    && node["delay_checked_at"]
                        .as_str()
                        .is_none_or(|t| t < checked)
                {
                    node["delay"] = result["delay"].clone();
                    node["delay_checked_at"] = json!(checked);
                    changed = true;
                }
            }
        }
    }
    if changed {
        secrets::put(s, KEY, &v).await?;
    }
    Ok(())
}

pub async fn delay(State(s): State<AppState>, ApiJson(p): ApiJson<Node>) -> Result<Json<Value>> {
    s.config.writable()?;
    Ok(Json(probe(&s, &p.node_id, true).await?.0))
}
#[derive(Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct Batch {
    node_ids: Option<Vec<String>>,
}
pub async fn batch(
    State(s): State<AppState>,
    value: Option<Json<Batch>>,
) -> Result<(StatusCode, Json<Value>)> {
    s.config.writable()?;
    let mut batches = s.proxy_batches.lock().await;
    while batches.len() > 4 {
        batches.pop_front();
    }
    if let Some(running) = batches.iter().find(|v| v["status"] == "running") {
        return Ok((StatusCode::ACCEPTED, Json(running.clone())));
    }
    let v = read(&s).await?;
    let all = nodes(&v)
        .filter_map(|n| n["id"].as_str().map(str::to_owned))
        .collect::<Vec<_>>();
    let ids = value
        .and_then(|v| v.0.node_ids)
        .unwrap_or_else(|| all.clone());
    if ids.is_empty()
        || ids.len() > 2000
        || ids.iter().collect::<std::collections::HashSet<_>>().len() != ids.len()
        || ids.iter().any(|id| !all.contains(id))
    {
        return Err(ApiError::bad("请选择 1–2000 个不同的现有节点"));
    }
    let id = uuid::Uuid::new_v4().simple().to_string();
    let result = json!({"batch_id":id,"status":"running","total":ids.len(),"completed":0,"succeeded":0,"failed":0,"started_at":stamp(),"finished_at":null,"message":"正在测速","results":ids.iter().map(|id|json!({"node_id":id,"status":"pending","delay":null,"ok":null,"message":"等待测速"})).collect::<Vec<_>>()});
    batches.push_back(result.clone());
    drop(batches);
    let task_state = s.clone();
    tokio::spawn(async move {
        let s = task_state;
        let id_ref = &id;
        let tasks=stream::iter(ids.into_iter().map(|node_id|{let s=s.clone();async move{let (result,signature)=probe(&s,&node_id,false).await.unwrap_or_else(|_|(json!({"node_id":node_id,"ok":false,"delay":null,"checked_at":stamp(),"message":"测速失败，请检查内核与节点"}),String::new()));(node_id,result,signature)}})).buffer_unordered(6);
        tokio::pin!(tasks);
        let mut saved = vec![];
        while let Some((node, result, signature)) = tasks.next().await {
            saved.push((node.clone(), result.clone(), signature));
            let mut batches = s.proxy_batches.lock().await;
            if let Some(batch) = batches.iter_mut().find(|v| v["batch_id"] == *id_ref) {
                if let Some(row) = batch["results"]
                    .as_array_mut()
                    .unwrap()
                    .iter_mut()
                    .find(|v| v["node_id"] == node)
                {
                    *row = result;
                    row["status"] = json!("completed");
                }
                batch["completed"] = json!(batch["completed"].as_u64().unwrap_or(0) + 1);
                let key = if batch["results"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|v| v["node_id"] == node)
                    .is_some_and(|v| v["ok"] == true)
                {
                    "succeeded"
                } else {
                    "failed"
                };
                batch[key] = json!(batch[key].as_u64().unwrap_or(0) + 1);
            }
        }
        let persisted = save_delays(&s, &saved).await.is_ok();
        let mut batches = s.proxy_batches.lock().await;
        if let Some(batch) = batches.iter_mut().find(|v| v["batch_id"] == id) {
            batch["status"] = json!("completed");
            batch["finished_at"] = json!(stamp());
            batch["message"] = json!(if persisted {
                "测速完成"
            } else {
                "测速完成，但结果保存失败，请检查数据库"
            });
        }
    });
    Ok((StatusCode::ACCEPTED, Json(result)))
}
pub async fn latest(State(s): State<AppState>) -> Json<Value> {
    Json(
        s.proxy_batches
            .lock()
            .await
            .back()
            .cloned()
            .unwrap_or(Value::Null),
    )
}
pub async fn batch_status(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>> {
    Ok(Json(
        s.proxy_batches
            .lock()
            .await
            .iter()
            .find(|v| v["batch_id"] == id)
            .cloned()
            .ok_or(ApiError::missing("测速批次不存在"))?,
    ))
}
pub struct Manager {
    stop: CancellationToken,
    task: tokio::task::JoinHandle<()>,
}

impl Manager {
    pub fn start(s: AppState) -> Self {
        let stop = CancellationToken::new();
        let token = stop.clone();
        let task = tokio::spawn(async move {
            let mut ticks = 0u64;
            loop {
                if token.is_cancelled() {
                    break;
                }
                if !s.config.read_only
                    && let Ok(v) = read(&s).await
                {
                    let mut c = s.proxy_core.lock().await;
                    if v["enabled"] == true
                        && !c.alive()
                        && c.last_restart
                            .is_none_or(|t| t.elapsed() > Duration::from_secs(30))
                    {
                        c.last_restart = Some(Instant::now());
                        c.restart_count += 1;
                        if c.apply(&v).await.is_err() {
                            c.error = Some("内核启动失败，将自动重试；不会回退直连");
                        }
                    }
                    publish(&s, &mut c);
                    drop(c);
                    if ticks.is_multiple_of(60) {
                        let due = v["subscriptions"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .filter(|sub| {
                                sub["auto_update"] == true
                                    && sub["next_update_at"]
                                        .as_str()
                                        .is_none_or(|t| t <= stamp().as_str())
                            })
                            .filter_map(|sub| sub["id"].as_str().map(str::to_owned))
                            .collect::<Vec<_>>();
                        for id in due {
                            tokio::select! {_=token.cancelled()=>break,_=modify(&s,&id,None,true)=>()}
                        }
                    }
                }
                ticks += 1;
                tokio::select! {_=token.cancelled()=>break,_=tokio::time::sleep(Duration::from_secs(1))=>()}
            }
            let mut c = s.proxy_core.lock().await;
            c.stop().await;
            publish(&s, &mut c);
        });
        Self { stop, task }
    }
    pub async fn stop(self) {
        self.stop.cancel();
        if tokio::time::timeout(Duration::from_secs(3), self.task)
            .await
            .is_err()
        { /* Child kill-on-drop also handles process termination. */ }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    #[ignore = "requires the pinned local Mihomo binary; no external traffic is sent"]
    async fn pinned_core_selects_only_the_requested_node_and_rejects_a_missing_selection() {
        let mut c = Core::default();
        let mut v = json!({"enabled":true,"selected_node_id":"q-fixture","subscriptions":[{"nodes":[{"id":"q-fixture","config":{"name":"fixture","type":"ss","server":"127.0.0.1","port":9,"cipher":"aes-128-gcm","password":"synthetic-only"}}]}]});
        c.apply(&v).await.unwrap();
        let group = c
            .request(
                reqwest::Method::GET,
                "/proxies/QIYE",
                None,
                Duration::from_secs(2),
            )
            .await
            .unwrap();
        assert_eq!(group["now"], "q-fixture");
        v["selected_node_id"] = Value::Null;
        c.apply(&v).await.unwrap();
        let group = c
            .request(
                reqwest::Method::GET,
                "/proxies/QIYE",
                None,
                Duration::from_secs(2),
            )
            .await
            .unwrap();
        assert_eq!(group["now"], "REJECT");
        c.stop().await;
        assert!(!c.alive());
    }
}
