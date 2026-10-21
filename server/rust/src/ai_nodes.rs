use crate::{
    ApiJson, AppState, content_store,
    db::{Tx, now, truth},
    error::{ApiError, Result},
    executor, network, platforms, queue, secrets,
    tasks::{self, Context, TaskResult},
};
use axum::{
    Json,
    extract::{OriginalUri, Path, State},
    http::Method,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{collections::BTreeMap, time::Duration};
pub const KEY: &str = "ai_controller_nodes";
pub fn defaults() -> Value {
    json!({"nodes":[],"image_node_id":null})
}
pub async fn config(s: &AppState) -> Result<Value> {
    let mut c = defaults();
    if let Some(v) = secrets::get(s, KEY).await? {
        c.as_object_mut()
            .unwrap()
            .extend(v.as_object().unwrap().clone());
    }
    Ok(c)
}
async fn config_tx(s: &AppState, tx: &mut Tx) -> Result<Value> {
    let mut c = defaults();
    c.as_object_mut().unwrap().extend(
        executor::setting_tx(s, tx, KEY)
            .await?
            .as_object()
            .unwrap()
            .clone(),
    );
    Ok(c)
}
pub fn find(c: &Value, id: &str, enabled: bool) -> Result<Value> {
    c["nodes"]
        .as_array()
        .and_then(|n| {
            n.iter()
                .find(|n| n["id"] == id && (!enabled || truth(&n["enabled"])))
        })
        .cloned()
        .ok_or_else(|| ApiError::bad("AI 节点不存在或已停用，未切换到其他节点"))
}
pub fn public(n: &Value) -> Value {
    json!({"id":n["id"],"label":n["label"],"base_url":n["base_url"],"enabled":truth(&n["enabled"]),"has_key":n["key"].as_str().is_some_and(|s|!s.is_empty())})
}
fn list_value(c: &Value) -> Value {
    json!({"items":c["nodes"].as_array().into_iter().flatten().map(public).collect::<Vec<_>>(),"image_node_id":c["image_node_id"]})
}
pub async fn list(State(s): State<AppState>) -> Result<Json<Value>> {
    Ok(Json(list_value(&config(&s).await?)))
}
pub fn node_url(raw: &str) -> Result<String> {
    let url = crate::devices::normalize_url(raw)?;
    let parsed = url::Url::parse(&url).map_err(|_| ApiError::bad("AI 地址无效"))?;
    if parsed
        .host_str()
        .and_then(|h| h.trim_matches(['[', ']']).parse::<std::net::IpAddr>().ok())
        .is_some_and(|ip| match ip {
            std::net::IpAddr::V4(a) => a.is_link_local(),
            std::net::IpAddr::V6(a) => a.is_unicast_link_local(),
        })
    {
        return Err(ApiError::bad("AI 服务不支持链路本地地址"));
    }
    Ok(url.trim_end_matches('/').into())
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Node {
    label: String,
    base_url: String,
    #[serde(default = "yes")]
    enabled: bool,
    #[serde(default)]
    key: Option<String>,
}
fn yes() -> bool {
    true
}
fn checked(n: Node) -> Result<Value> {
    let label = n.label.trim();
    if label.is_empty() || label.chars().count() > 80 {
        return Err(ApiError::invalid("AI 节点名称不能为空且最多 80 字"));
    }
    if n.key.as_ref().is_some_and(|s| {
        s.len() < 32
            || s.len() > 256
            || !s
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
    }) {
        return Err(ApiError::invalid(
            "控制器密钥须为 32–256 位字母、数字、下划线或短横线",
        ));
    }
    Ok(json!({"label":label,"base_url":node_url(&n.base_url)?,"enabled":n.enabled,"key":n.key}))
}
pub async fn add(State(s): State<AppState>, ApiJson(n): ApiJson<Node>) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut n = checked(n)?;
    if n["key"].is_null() {
        return Err(ApiError::bad("添加节点需要控制器密钥"));
    }
    n["id"] = json!(uuid::Uuid::new_v4().to_string());
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let mut c = config_tx(&s, &mut tx).await?;
    if c["nodes"].as_array().unwrap().len() >= 100 {
        return Err(ApiError::bad("最多保存 100 个 AI 节点"));
    }
    c["nodes"].as_array_mut().unwrap().push(n.clone());
    secrets::put_tx(&s, &mut tx, KEY, &c).await?;
    tx.commit().await?;
    Ok(Json(public(&n)))
}
pub async fn update(
    State(s): State<AppState>,
    Path(id): Path<String>,
    ApiJson(n): ApiJson<Node>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut n = checked(n)?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let mut c = config_tx(&s, &mut tx).await?;
    let old = find(&c, &id, false)?;
    if old["base_url"] != n["base_url"] && n["key"].is_null() {
        return Err(ApiError::bad("更换控制器地址时请重新输入密钥"));
    }
    if n["key"].is_null() {
        n["key"] = old["key"].clone();
    }
    n["id"] = json!(id);
    let row = c["nodes"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|v| v["id"] == id)
        .unwrap();
    *row = n.clone();
    secrets::put_tx(&s, &mut tx, KEY, &c).await?;
    tx.commit().await?;
    Ok(Json(public(&n)))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Routing {
    image_node_id: Option<String>,
}
pub async fn routing(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<Routing>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let mut c = config_tx(&s, &mut tx).await?;
    if let Some(id) = &v.image_node_id {
        find(&c, id, true)?;
    }
    c["image_node_id"] = json!(v.image_node_id);
    secrets::put_tx(&s, &mut tx, KEY, &c).await?;
    tx.commit().await?;
    Ok(Json(list_value(&c)))
}
pub async fn remove(State(s): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let mut c = config_tx(&s, &mut tx).await?;
    if c["image_node_id"] == id
        || executor::setting_tx(&s, &mut tx, "metadata_translation").await?["controller_node_id"]
            == id
    {
        return Err(ApiError(
            axum::http::StatusCode::CONFLICT,
            "请先将图片识别或汉化切换到其他节点",
        ));
    }
    find(&c, &id, false)?;
    c["nodes"].as_array_mut().unwrap().retain(|n| n["id"] != id);
    secrets::put_tx(&s, &mut tx, KEY, &c).await?;
    tx.commit().await?;
    Ok(Json(json!({"ok":true})))
}
pub async fn request(
    node: &Value,
    method: Method,
    path: &str,
    body: Option<Value>,
    bytes: Option<Vec<u8>>,
    timeout: u64,
) -> Result<Value> {
    let root = node_url(node["base_url"].as_str().unwrap_or(""))?;
    let key = node["key"]
        .as_str()
        .ok_or_else(|| ApiError::bad("AI 控制器密钥未配置"))?;
    let mut req = network::direct_client(Duration::from_secs(timeout))?
        .request(method, format!("{root}{path}"))
        .header(
            reqwest::header::AUTHORIZATION,
            platforms::sensitive(&format!("Bearer {key}"))?,
        )
        .header(reqwest::header::ACCEPT, "application/json");
    if let Some(bytes) = bytes {
        req = req
            .header(reqwest::header::CONTENT_TYPE, "application/octet-stream")
            .body(bytes);
    } else if let Some(body) = body {
        req = req.json(&body);
    }
    let response = req
        .send()
        .await
        .map_err(|_| ApiError::bad("无法连接 AI 控制器，请检查地址与网络"))?;
    if !matches!(response.status().as_u16(), 200..=202) {
        return Err(ApiError::bad("AI 控制器请求失败，请检查密钥、容器和模型"));
    }
    let raw = network::bounded(response, 256 * 1024).await?;
    let value: Value =
        serde_json::from_slice(&raw).map_err(|_| ApiError::bad("AI 控制器返回无效数据"))?;
    if !value.is_object() {
        return Err(ApiError::bad("AI 控制器响应格式无效"));
    }
    Ok(value)
}
pub async fn relay(
    State(s): State<AppState>,
    Path(p): Path<BTreeMap<String, String>>,
    OriginalUri(uri): OriginalUri,
    method: Method,
    body: Option<Json<Value>>,
) -> Result<Json<Value>> {
    let id = p
        .get("id")
        .ok_or_else(|| ApiError::bad("缺少 AI 节点编号"))?;
    let n = find(&config(&s).await?, id, true)?;
    let suffix = uri
        .path()
        .strip_prefix(&format!("/api/ai/nodes/{id}"))
        .ok_or_else(|| ApiError::bad("AI 路径无效"))?;
    let mut timeout = 20;
    let path = match suffix {
        "/test" => "/v1/status".into(),
        "/vision" => "/v1/vision/status".into(),
        "/models" | "/models/pull" | "/models/unload" => {
            if method != Method::GET {
                let v = body
                    .as_ref()
                    .map(|b| &b.0["model"])
                    .and_then(Value::as_str)
                    .ok_or_else(|| ApiError::invalid("请填写模型标识"))?;
                model(v)?;
                s.config.writable()?;
            }
            if suffix.ends_with("unload") {
                timeout = 100;
            }
            format!("/v1/ollama{suffix}")
        }
        "/operations" => "/v1/operations".into(),
        _ if suffix.starts_with("/operations/") => {
            let op = p
                .get("operation")
                .ok_or_else(|| ApiError::bad("任务编号无效"))?;
            uuid::Uuid::parse_str(op).map_err(|_| ApiError::invalid("任务编号无效"))?;
            format!("/v1/operations/{op}")
        }
        _ if suffix.starts_with("/services/") => {
            s.config.writable()?;
            let service = p.get("service").map(String::as_str).unwrap_or("");
            let action = p.get("action").map(String::as_str).unwrap_or("");
            if !["ollama", "vision"].contains(&service)
                || !["start", "stop", "restart"].contains(&action)
            {
                return Err(ApiError::invalid("AI 服务或操作无效"));
            }
            timeout = 35;
            format!("/v1/services/{service}/{action}")
        }
        _ => return Err(ApiError::bad("未知 AI 控制器操作")),
    };
    let data = request(
        &n,
        if suffix == "/test" {
            Method::GET
        } else {
            method
        },
        &path,
        body.map(|b| b.0),
        None,
        timeout,
    )
    .await?;
    if suffix == "/test" && data["protocol"] != "qiye-ai-controller-v1" {
        return Err(ApiError::bad("地址不是兼容的 AI 控制器"));
    }
    Ok(Json(data))
}
pub fn model(name: &str) -> Result<()> {
    if name.len() > 191
        || !regex::Regex::new(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,190}$")
            .unwrap()
            .is_match(name)
    {
        return Err(ApiError::invalid("模型标识格式无效"));
    }
    Ok(())
}
pub async fn image_target(s: &AppState) -> Result<Option<Value>> {
    let c = config(s).await?;
    image_target_config(&c)
}
fn image_target_config(c: &Value) -> Result<Option<Value>> {
    if let Some(id) = c["image_node_id"].as_str() {
        return Ok(Some(find(c, id, true)?));
    }
    let url = std::env::var("LIBRARY_AI_VISION_URL").unwrap_or_default();
    let key = std::env::var("LIBRARY_AI_VISION_KEY").unwrap_or_default();
    if url.is_empty() {
        return Ok(None);
    }
    Ok(Some(
        json!({"id":"local-independent-vision","base_url":node_url(&url)?,"label":"本机独立图像服务","key":key,"enabled":true}),
    ))
}
pub async fn status(State(s): State<AppState>) -> Result<Json<Value>> {
    let target = image_target(&s).await?;
    let available = target.is_some();
    Ok(Json(
        json!({"available":available,"models":[{"id":"remote-wd-tagger","label":"标签／角色／R18 分级","available":available,"message":if available{"已配置独立图像服务，可测试连接确认模型状态"}else{"请配置独立图像服务或选择 AI 控制器节点"}}],"provider":"remote","requested_provider":"remote","actual_provider":null,"device":target.as_ref().map(|t|t["label"].clone()),"available_providers":[],"execution_state":if available{"configured"}else{"unavailable"},"message":"图像识别在独立模型服务中执行，失败不会自动切换节点"}),
    ))
}
#[derive(Deserialize)]
pub struct Analyze {
    item_ids: Vec<String>,
}
pub async fn analyze(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<Analyze>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    if v.item_ids.is_empty() || v.item_ids.len() > 200 {
        return Err(ApiError::invalid("每次选择 1–200 个作品"));
    }
    let mut tx = s.db.begin().await?;
    queue::locked(&s, &mut tx).await?;
    let mut jobs = vec![];
    let mut seen = std::collections::HashSet::new();
    for id in v.item_ids {
        if !seen.insert(id.clone()) {
            continue;
        }
        if tx
            .query("SELECT id FROM items WHERE id=?", &[json!(id)])
            .await?
            .is_empty()
        {
            return Err(ApiError::missing("作品不存在"));
        }
        jobs.push(queue::job_view(
            tasks::enqueue_tx(&s, &mut tx, "ai", &json!({"item_id":id})).await?,
        ));
    }
    tx.commit().await?;
    Ok(Json(json!({"jobs":jobs})))
}
pub fn prediction(v: &Value) -> Result<()> {
    let ratings = v["rating_scores"]
        .as_object()
        .filter(|m| !m.is_empty() && m.len() <= 4)
        .ok_or_else(|| ApiError::bad("图像模型分级结果不完整"))?;
    if ratings.iter().any(|(k, v)| {
        !["general", "sensitive", "questionable", "explicit"].contains(&k.as_str())
            || !v
                .as_f64()
                .is_some_and(|n| n.is_finite() && (0.0..=1.0).contains(&n))
    }) {
        return Err(ApiError::bad("图像模型分级分数无效"));
    }
    let (strongest, score) = ratings
        .iter()
        .max_by(|a, b| a.1.as_f64().unwrap().total_cmp(&b.1.as_f64().unwrap()))
        .unwrap();
    let expected = if score.as_f64().unwrap() < 0.6 {
        "unknown"
    } else {
        match strongest.as_str() {
            "general" => "general",
            "sensitive" => "sensitive",
            _ => "adult",
        }
    };
    if v["rating"] != expected
        || v["model_version"]
            .as_str()
            .is_none_or(|s| s.is_empty() || s.chars().count() > 100)
    {
        return Err(ApiError::bad("图像模型分级与证据不一致"));
    }
    let tags = v["tags"]
        .as_array()
        .filter(|v| v.len() <= 100)
        .ok_or_else(|| ApiError::bad("图像标签结果无效"))?;
    for t in tags {
        if t["name"]
            .as_str()
            .is_none_or(|s| s.is_empty() || s.chars().count() > 191)
            || !t["score"]
                .as_f64()
                .is_some_and(|n| n.is_finite() && (0.0..=1.0).contains(&n))
            || ![Some(0), Some(4)].contains(&t["category"].as_i64())
        {
            return Err(ApiError::bad("图像标签结果格式无效"));
        }
    }
    Ok(())
}
pub async fn process(ctx: Context, p: &Value) -> TaskResult<Value> {
    let s = &ctx.state;
    let id = p["item_id"].as_str().unwrap_or("");
    let assets =
        s.db.query(
            "SELECT * FROM assets WHERE item_id=? ORDER BY position",
            &[json!(id)],
        )
        .await?;
    if assets.is_empty() {
        return Err(ApiError::bad("作品仅含元数据，请先保存原文件再分析").into());
    }
    let images = assets
        .iter()
        .filter(|a| {
            a["mime_type"]
                .as_str()
                .is_some_and(|m| m.starts_with("image/"))
        })
        .collect::<Vec<_>>();
    let texts = assets
        .iter()
        .filter(|a| {
            a["mime_type"]
                .as_str()
                .is_some_and(|m| m.starts_with("text/"))
        })
        .collect::<Vec<_>>();
    if images.is_empty() && texts.is_empty() {
        return Err(ApiError::bad("没有可分析的图片或文本文件").into());
    }
    let target = if images.is_empty() {
        None
    } else {
        Some(
            image_target(s)
                .await?
                .ok_or_else(|| ApiError::bad("请配置独立图像识别服务"))?,
        )
    };
    let mut evidence = vec![];
    let mut text_evidence = vec![];
    let mut combined: BTreeMap<String, Value> = BTreeMap::new();
    let mut suggestion: Option<String> = None;
    for (index, a) in images.iter().enumerate() {
        ctx.checkpoint(Some(5.0 + 80.0 * index as f64 / images.len() as f64))
            .await?;
        let root = s.config.storage_root.clone();
        let relative = a["relative_path"].as_str().unwrap_or("").to_owned();
        let bytes = crate::media::io(s, move || {
            use std::io::Read;
            let path = crate::media::resolve(&root, &relative)?;
            let mut bytes = vec![];
            std::fs::File::open(path)?
                .take(128 * 1024 * 1024 + 1)
                .read_to_end(&mut bytes)?;
            if bytes.len() > 128 * 1024 * 1024 {
                return Err(ApiError::bad("图像超过大小限制"));
            }
            Ok(bytes)
        })
        .await?;
        let bytes = if bytes.len() > 16 * 1024 * 1024 {
            let permit = s
                .cpu_limit
                .clone()
                .acquire_owned()
                .await
                .map_err(|_| ApiError::bad("图像处理器不可用"))?;
            tokio::task::spawn_blocking(move || {
                let _permit = permit;
                let image = content_store::picture(&bytes)?
                    .0
                    .thumbnail(2048, 2048)
                    .to_rgb8();
                let mut encoded = vec![];
                image::codecs::jpeg::JpegEncoder::new_with_quality(&mut encoded, 95)
                    .encode_image(&image)
                    .map_err(|_| ApiError::bad("图片缩小失败"))?;
                Ok::<_, ApiError>(encoded)
            })
            .await
            .map_err(|_| ApiError::bad("图片预处理失败"))??
        } else {
            bytes
        };
        let result = request(
            target.as_ref().unwrap(),
            Method::POST,
            "/v1/vision/analyze",
            None,
            Some(bytes),
            190,
        )
        .await?;
        prediction(&result)?;
        let rating = result["rating"].as_str().unwrap();
        let rank = |v: &str| match v {
            "adult" => 3,
            "unknown" => 2,
            "sensitive" => 1,
            _ => 0,
        };
        if suggestion.as_ref().is_none_or(|v| rank(rating) > rank(v)) {
            suggestion = Some(rating.into());
        }
        evidence.push(json!({"asset_id":a["id"],"position":a["position"],"rating":rating,"rating_scores":result["rating_scores"],"model_version":result["model_version"],"execution":result["execution"]}));
        for tag in result["tags"].as_array().unwrap() {
            let name = tag["name"].as_str().unwrap().to_owned();
            if combined
                .get(&name)
                .is_none_or(|t| t["score"].as_f64() < tag["score"].as_f64())
            {
                let mut t = tag.clone();
                t["asset_id"] = a["id"].clone();
                t["model_version"] = result["model_version"].clone();
                combined.insert(name, t);
            }
        }
    }
    const RULES: [(&str, &[&str]); 5] = [
        (
            "分类:科技",
            &[
                "编程",
                "算法",
                "数据库",
                "人工智能",
                "python",
                "software",
                "technology",
            ],
        ),
        (
            "分类:历史",
            &["历史", "朝代", "考古", "史料", "history", "historical"],
        ),
        (
            "分类:文学",
            &["小说", "诗歌", "散文", "文学", "fiction", "poetry"],
        ),
        (
            "分类:生活",
            &["食谱", "烹饪", "园艺", "旅行", "生活", "recipe", "cooking"],
        ),
        (
            "分类:艺术",
            &["绘画", "摄影", "艺术", "插画", "painting", "photography"],
        ),
    ];
    for a in &texts {
        ctx.checkpoint(Some(90.0)).await?;
        let root = s.config.storage_root.clone();
        let relative = a["relative_path"].as_str().unwrap_or("").to_owned();
        let text = crate::media::io(s, move || {
            use std::io::Read;
            let mut data = vec![];
            std::fs::File::open(crate::media::resolve(&root, &relative)?)?
                .take(16 * 1024 * 1024 + 1)
                .read_to_end(&mut data)?;
            if data.len() > 16 * 1024 * 1024 {
                return Err(ApiError::bad("文本超过分类限制"));
            }
            Ok(String::from_utf8_lossy(&data).to_lowercase())
        })
        .await?;
        for (name, keywords) in RULES {
            let hits = keywords
                .iter()
                .filter(|k| text.contains(**k))
                .copied()
                .collect::<Vec<_>>();
            if !hits.is_empty() {
                combined.insert(name.into(),json!({"name":name,"score":1.0,"asset_id":a["id"],"model_version":"text-rules-v1"}));
                text_evidence.push(json!({"asset_id":a["id"],"tag":name,"matched_keywords":hits}));
            }
        }
    }
    let mut tx = s.db.begin().await?;
    ctx.guard_tx(&mut tx).await?;
    if tx
        .query("SELECT id FROM items WHERE id=?", &[json!(id)])
        .await?
        .is_empty()
    {
        return Err(ApiError::bad("作品已被删除").into());
    }
    if target.is_some() && image_target_config(&config_tx(s, &mut tx).await?)? != target {
        return Err(ApiError::bad("图像 AI 节点配置已更改，结果未保存").into());
    }
    tx.execute(
        "DELETE FROM item_tags WHERE item_id=? AND origin='ai'",
        &[json!(id)],
    )
    .await?;
    for (name, t) in &combined {
        let tag = content_store::ensure_tag(s, &mut tx, name).await?;
        tx.execute(
            "INSERT INTO item_tags(item_id,tag_id,origin,score,model_version) VALUES(?,?,'ai',?,?)",
            &[
                json!(id),
                json!(tag),
                t["score"].clone(),
                t["model_version"].clone(),
            ],
        )
        .await?;
    }
    let mut applied = false;
    if let Some(rating) = &suggestion {
        applied=tx.execute("UPDATE items SET rating=?,rating_origin='ai',updated_at=? WHERE id=? AND rating_origin!='manual' AND (?='adult' OR rating_origin!='source' OR rating!='adult')",&[json!(rating),json!(now()),json!(id),json!(rating)]).await?>0;
    }
    tx.commit().await?;
    Ok(
        json!({"item_id":id,"analyzed_pages":images.len(),"analyzed_texts":texts.len(),"complete":true,"suggested_rating":suggestion,"rating_applied":applied,"tags":combined.values().collect::<Vec<_>>(),"page_evidence":evidence,"text_evidence":text_evidence,"message":"分析完成"}),
    )
}
