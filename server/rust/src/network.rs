use crate::{
    ApiJson, AppState,
    auth::DeviceId,
    error::{ApiError, Result},
    secrets,
};
use axum::{
    Json,
    extract::{ConnectInfo, Extension, Path, State},
};
use reqwest::{Client, Method};
use serde::Deserialize;
use serde_json::{Value, json};
use std::sync::Arc;
use std::{
    collections::HashMap,
    net::{IpAddr, SocketAddr},
    time::{Duration, Instant},
};
struct PublicResolver {
    proxy_host: Option<String>,
}
impl reqwest::dns::Resolve for PublicResolver {
    fn resolve(&self, name: reqwest::dns::Name) -> reqwest::dns::Resolving {
        let host = name.as_str().to_owned();
        let proxy = self.proxy_host.as_deref() == Some(host.as_str());
        Box::pin(async move {
            let answers = tokio::net::lookup_host((host.as_str(), 443))
                .await?
                .collect::<Vec<_>>();
            if answers.is_empty() || !proxy && answers.iter().any(|a| !public_ip(a.ip())) {
                return Err(Box::new(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    "network target is not public",
                ))
                    as Box<dyn std::error::Error + Send + Sync>);
            }
            Ok(Box::new(answers.into_iter()) as reqwest::dns::Addrs)
        })
    }
}
const PROXY_KEY: &str = "outbound_proxy";
pub const ENDPOINTS: [(&str, &str, &str, &str); 5] = [
    (
        "pixiv",
        "Pixiv",
        "https://app-api.pixiv.net",
        "作品 API 地址；账号授权仍使用 Pixiv 官方 OAuth 地址。",
    ),
    (
        "picacg",
        "哔咔漫画",
        "https://picaapi.picacomic.com",
        "漫画 API 地址；请求签名保持平台原有格式。",
    ),
    (
        "ehentai",
        "E-Hentai",
        "https://api.e-hentai.org/api.php",
        "仅切换画廊元数据 API；画廊页面和图片来源保持不变。",
    ),
    (
        "jm",
        "JM 天堂",
        "https://18comic.vip",
        "作品 HTML 接口域名，只支持站点根地址。",
    ),
    (
        "kemono",
        "Kemono",
        "https://kemono.cr/api/v1",
        "作者与作品 API 地址。",
    ),
];
pub fn platform(p: &str) -> Result<(&'static str, &'static str, &'static str)> {
    ENDPOINTS
        .iter()
        .find(|(id, _, _, _)| *id == p)
        .map(|(_, label, root, description)| (*label, *root, *description))
        .ok_or_else(|| ApiError::missing("不支持的平台接口"))
}
pub fn public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(a) => {
            let b = a.octets();
            !a.is_private()
                && !a.is_loopback()
                && !a.is_link_local()
                && !a.is_unspecified()
                && !a.is_multicast()
                && !a.is_documentation()
                && !a.is_broadcast()
                && b[0] != 0
                && b[0] < 240
                && !(b[0] == 100 && (64..=127).contains(&b[1]))
                && !(b[0] == 198 && (b[1] == 18 || b[1] == 19))
                && !(b[0] == 192 && b[1] == 0 && b[2] == 0)
        }
        IpAddr::V6(a) => {
            if let Some(v4) = a.to_ipv4_mapped() {
                return public_ip(IpAddr::V4(v4));
            }
            let s = a.segments();
            !(a.is_loopback()
                || a.is_unspecified()
                || a.is_multicast()
                || a.is_unique_local()
                || a.is_unicast_link_local()
                || (s[0] == 0x2001 && s[1] == 0x0db8))
                && !(s[0] == 0x2001 && s[1] < 0x200)
                && s[0] != 0x2002
                && (s[0] & 0xffc0) != 0xfec0
        }
    }
}
pub async fn public_dns(host: &str) -> Result<Vec<SocketAddr>> {
    let answers =
        tokio::time::timeout(Duration::from_secs(5), tokio::net::lookup_host((host, 443)))
            .await
            .map_err(|_| ApiError::bad("无法解析接口域名，请检查地址和网络"))?
            .map_err(|_| ApiError::bad("无法解析接口域名，请检查地址和网络"))?
            .collect::<Vec<_>>();
    if answers.is_empty() || answers.iter().any(|a| !public_ip(a.ip())) {
        return Err(ApiError::bad("接口域名必须解析到公网地址"));
    }
    Ok(answers)
}
pub fn validate_api(p: &str, raw: &str) -> Result<String> {
    platform(p)?;
    let bad = || {
        ApiError::bad(
            "接口地址须为公网 HTTPS 地址，不能含账号、端口、查询参数或片段；JM 只支持根地址",
        )
    };
    let raw = raw.trim();
    if raw.is_empty()
        || raw.len() > 2048
        || raw.chars().any(|c| c.is_whitespace() || c.is_control())
        || raw.contains(['\\', '%', '?', '#'])
    {
        return Err(bad());
    }
    let raw_path = raw
        .split_once("://")
        .map(|(_, s)| s.split_once('/').map(|(_, p)| p).unwrap_or(""))
        .ok_or_else(bad)?;
    if raw_path.split('/').any(|p| matches!(p, "." | ".."))
        || !raw_path
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"/._~-".contains(&b))
    {
        return Err(bad());
    }
    let u = url::Url::parse(raw).map_err(|_| bad())?;
    let host = u.host_str().ok_or_else(bad)?;
    if u.scheme() != "https"
        || !u.username().is_empty()
        || u.password().is_some()
        || u.port().is_some_and(|p| p != 443)
        || host.ends_with('.')
        || !host
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".-".contains(&b))
        || !host.contains('.')
        || [".localhost", ".local", ".internal", ".test", ".invalid"]
            .iter()
            .any(|s| host.ends_with(s))
        || p == "jm" && u.path() != "/"
    {
        return Err(bad());
    }
    if host.parse::<IpAddr>().is_ok_and(|ip| !public_ip(ip)) {
        return Err(bad());
    }
    Ok(format!(
        "https://{}{}",
        host.to_lowercase(),
        u.path().trim_end_matches('/')
    ))
}
pub async fn endpoint(s: &AppState, p: &str) -> Result<String> {
    let (_, default, _) = platform(p)?;
    let c = secrets::get(s, &format!("platform_endpoint:{p}"))
        .await?
        .unwrap_or_else(|| json!({"enabled":false,"custom_url":null}));
    if c["enabled"] == true {
        validate_api(p, c["custom_url"].as_str().unwrap_or(""))
    } else {
        Ok(default.to_owned())
    }
}
async fn public_endpoint(s: &AppState, p: &str) -> Result<Value> {
    let (label, default, description) = platform(p)?;
    let c = secrets::get(s, &format!("platform_endpoint:{p}"))
        .await?
        .unwrap_or_else(|| json!({"enabled":false,"custom_url":null}));
    Ok(
        json!({"platform":p,"label":label,"default_url":default,"custom_url":c["custom_url"],"enabled":c["enabled"]==true,"effective_url":endpoint(s,p).await?,"description":description}),
    )
}
pub async fn endpoints(State(s): State<AppState>) -> Result<Json<Value>> {
    let mut items = vec![];
    for (p, _, _, _) in ENDPOINTS {
        items.push(public_endpoint(&s, p).await?);
    }
    Ok(Json(json!({"items":items})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EndpointInput {
    custom_url: Option<String>,
    enabled: bool,
    admin_password: Option<String>,
}
pub async fn set_endpoint(
    State(s): State<AppState>,
    Path(p): Path<String>,
    device: Option<Extension<DeviceId>>,
    connect: Option<Extension<ConnectInfo<SocketAddr>>>,
    ApiJson(v): ApiJson<EndpointInput>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    platform(&p)?;
    if device.is_some() {
        let password = v.admin_password.as_deref().ok_or(ApiError(
            axum::http::StatusCode::FORBIDDEN,
            "更改平台接口需要输入当前管理员密码",
        ))?;
        s.rates
            .password(
                format!(
                    "login:{}",
                    connect
                        .map(|Extension(ConnectInfo(a))| a.ip().to_string())
                        .unwrap_or_default()
                ),
                password,
                &s.config.password,
            )
            .map_err(|e| {
                if e.0 == axum::http::StatusCode::UNAUTHORIZED {
                    ApiError(
                        axum::http::StatusCode::FORBIDDEN,
                        "管理员密码错误；设备登录仍然有效",
                    )
                } else {
                    e
                }
            })?;
    }
    let custom = v
        .custom_url
        .as_deref()
        .filter(|s| !s.trim().is_empty())
        .map(|url| validate_api(&p, url))
        .transpose()?;
    if v.enabled {
        let u = custom
            .as_deref()
            .ok_or_else(|| ApiError::bad("启用自定义接口前请填写地址"))?;
        public_dns(url::Url::parse(u).unwrap().host_str().unwrap()).await?;
    }
    secrets::put(
        &s,
        &format!("platform_endpoint:{p}"),
        &json!({"custom_url":custom,"enabled":v.enabled}),
    )
    .await?;
    Ok(Json(public_endpoint(&s, &p).await?))
}
fn default_proxy() -> Value {
    json!({"enabled":"false","proxy_url":"","proxy_username":"","proxy_password":"","controller_url":"","controller_secret":""})
}
pub async fn proxy_config(s: &AppState) -> Result<Value> {
    let mut c = default_proxy();
    if let Some(v) = secrets::get(s, PROXY_KEY).await? {
        for (k, v) in v
            .as_object()
            .ok_or_else(|| ApiError::bad("代理配置格式无效"))?
        {
            c[k] = v.clone();
        }
    }
    Ok(c)
}
fn public_proxy(c: &Value) -> Value {
    json!({"enabled":c["enabled"]=="true","proxy_url":c["proxy_url"],"proxy_username":c["proxy_username"],"has_proxy_password":c["proxy_password"].as_str().is_some_and(|s|!s.is_empty()),"controller_url":c["controller_url"],"has_controller_secret":c["controller_secret"].as_str().is_some_and(|s|!s.is_empty())})
}
pub fn validate_proxy(raw: &str, controller: bool) -> Result<String> {
    if raw.trim().is_empty() {
        return Ok(String::new());
    }
    let bad = || ApiError::bad("代理或控制器地址无效，认证信息请填独立输入框");
    let raw = raw.trim();
    if raw.len() > 2048
        || raw.contains(['?', '#', '\\'])
        || raw.chars().any(|c| c.is_whitespace() || c.is_control())
    {
        return Err(bad());
    }
    let u = url::Url::parse(raw).map_err(|_| bad())?;
    let authority = raw
        .split_once("://")
        .map(|(_, v)| v.trim_end_matches('/'))
        .ok_or_else(bad)?;
    if !authority
        .rsplit_once(':')
        .is_some_and(|(_, p)| p.parse::<u16>().is_ok_and(|p| p > 0))
        || u.host_str().is_none()
        || !u.username().is_empty()
        || u.password().is_some()
        || u.path() != "/" && u.path() != ""
        || if controller {
            !matches!(u.scheme(), "http" | "https")
        } else {
            !matches!(u.scheme(), "http" | "https" | "socks5" | "socks5h")
        }
    {
        return Err(bad());
    }
    Ok(raw.trim_end_matches('/').to_owned())
}
pub async fn proxy(State(s): State<AppState>) -> Result<Json<Value>> {
    Ok(Json(public_proxy(&proxy_config(&s).await?)))
}
#[derive(Default, Deserialize)]
#[serde(default)]
pub struct ProxyInput {
    enabled: bool,
    proxy_url: String,
    proxy_username: String,
    proxy_password: Option<String>,
    controller_url: String,
    controller_secret: Option<String>,
}
pub async fn set_proxy(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<ProxyInput>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let mut c = proxy_config(&s).await?;
    c["enabled"] = json!(if v.enabled { "true" } else { "false" });
    c["proxy_url"] = json!(validate_proxy(&v.proxy_url, false)?);
    c["controller_url"] = json!(validate_proxy(&v.controller_url, true)?);
    c["proxy_username"] = json!(v.proxy_username);
    if let Some(v) = v.proxy_password {
        if v.len() > 2048 {
            return Err(ApiError::invalid("代理密码过长"));
        }
        c["proxy_password"] = json!(v);
    }
    if let Some(v) = v.controller_secret {
        if v.len() > 2048 || v.chars().any(char::is_control) {
            return Err(ApiError::invalid("控制器密钥无效"));
        }
        c["controller_secret"] = json!(v);
    }
    if v.enabled && c["proxy_url"] == "" || c["proxy_password"] != "" && c["proxy_username"] == "" {
        return Err(ApiError::bad("启用代理需要地址；设置密码时需要用户名"));
    }
    secrets::put(&s, PROXY_KEY, &c).await?;
    Ok(Json(public_proxy(&c)))
}
pub async fn outbound_proxy(s: &AppState) -> Result<Option<String>> {
    if secrets::mapped(s, "managed_proxy", |v| v["enabled"] == true)
        .await?
        .unwrap_or(false)
    {
        return Ok(Some(format!(
            "http://127.0.0.1:{}",
            std::env::var("LIBRARY_MIHOMO_PROXY_PORT").unwrap_or_else(|_| "17890".to_owned())
        )));
    }
    let c = proxy_config(s).await?;
    if c["enabled"] != "true" {
        return Ok(None);
    }
    let address = validate_proxy(c["proxy_url"].as_str().unwrap_or(""), false)?;
    if address.is_empty() {
        return Err(ApiError::bad("代理已启用但地址缺失，请重新保存代理配置"));
    }
    let mut u = url::Url::parse(&address).map_err(|_| ApiError::bad("代理配置无效"))?;
    if c["proxy_username"].as_str().is_some_and(|s| !s.is_empty()) {
        u.set_username(c["proxy_username"].as_str().unwrap())
            .map_err(|_| ApiError::bad("代理认证无效"))?;
        u.set_password(c["proxy_password"].as_str())
            .map_err(|_| ApiError::bad("代理认证无效"))?;
    }
    Ok(Some(u.to_string().replacen("socks5://", "socks5h://", 1)))
}
/// Workers must not consume platform jobs while the managed core is starting
/// or restarting. This is only a bounded loopback readiness check, never a
/// platform request or a direct-network fallback.
pub async fn managed_ready(s: &AppState) -> Result<bool> {
    if !secrets::mapped(s, "managed_proxy", |v| v["enabled"] == true)
        .await?
        .unwrap_or(false)
    {
        return Ok(true);
    }
    let port = std::env::var("LIBRARY_MIHOMO_PROXY_PORT")
        .unwrap_or_else(|_| "17890".into())
        .parse::<u16>()
        .ok()
        .filter(|p| *p >= 1024)
        .ok_or_else(|| ApiError::bad("内置代理端口无效"))?;
    Ok(tokio::time::timeout(
        Duration::from_millis(300),
        tokio::net::TcpStream::connect((std::net::Ipv4Addr::LOCALHOST, port)),
    )
    .await
    .is_ok_and(|r| r.is_ok()))
}
pub fn direct_client(timeout: Duration) -> Result<Client> {
    Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(timeout)
        .connect_timeout(Duration::from_secs(10))
        .pool_max_idle_per_host(8)
        .build()
        .map_err(|_| ApiError::bad("无法初始化网络连接"))
}
pub async fn content_client(s: &AppState) -> Result<Client> {
    content_client_cookies(s, None).await
}
pub fn public_client(timeout: Duration) -> Result<Client> {
    Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(timeout)
        .connect_timeout(Duration::from_secs(10))
        .dns_resolver(Arc::new(PublicResolver { proxy_host: None }))
        .build()
        .map_err(|_| ApiError::bad("无法初始化公网连接"))
}
pub async fn content_client_cookies(
    s: &AppState,
    jar: Option<Arc<reqwest::cookie::Jar>>,
) -> Result<Client> {
    let mut b = Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(40))
        .connect_timeout(Duration::from_secs(12))
        .pool_max_idle_per_host(4)
        .user_agent("QiyeLibrary/0.1");
    let proxy = outbound_proxy(s).await?;
    let proxy_host = proxy
        .as_ref()
        .and_then(|p| url::Url::parse(p).ok())
        .and_then(|u| u.host_str().map(str::to_owned));
    b = b.dns_resolver(Arc::new(PublicResolver { proxy_host }));
    if let Some(p) = proxy {
        b = b.proxy(reqwest::Proxy::all(p).map_err(|_| ApiError::bad("代理配置无效"))?);
    }
    if let Some(jar) = jar {
        b = b.cookie_provider(jar);
    }
    b.build().map_err(|_| ApiError::bad("无法初始化平台连接"))
}
pub fn validate_url(raw: &str, hosts: &[String]) -> Result<url::Url> {
    let u = url::Url::parse(raw).map_err(|_| ApiError::bad("来源链接必须是该平台的 HTTPS 地址"))?;
    let host = u.host_str().unwrap_or("");
    if u.scheme() != "https"
        || !u.username().is_empty()
        || u.password().is_some()
        || u.port().is_some_and(|p| p != 443)
        || raw.contains('\\')
        || !hosts
            .iter()
            .any(|h| host == h || host.ends_with(&format!(".{h}")))
    {
        return Err(ApiError::bad("来源链接必须是该平台的 HTTPS 地址"));
    }
    Ok(u)
}
pub async fn bounded(mut response: reqwest::Response, limit: usize) -> Result<Vec<u8>> {
    if response.content_length().is_some_and(|n| n > limit as u64) {
        return Err(ApiError::bad("远程响应超过大小限制"));
    }
    let mut out = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| ApiError::bad("平台连接失败，请检查网络后重试"))?
    {
        if out.len() + chunk.len() > limit {
            return Err(ApiError::bad("远程响应超过大小限制"));
        }
        out.extend_from_slice(&chunk);
    }
    Ok(out)
}
pub async fn controller(
    c: &Value,
    method: Method,
    path: &str,
    body: Option<&Value>,
) -> Result<Value> {
    let root = validate_proxy(c["controller_url"].as_str().unwrap_or(""), true)?;
    if root.is_empty() {
        return Err(ApiError::bad("请先保存 Clash 控制器地址"));
    }
    let mut req = direct_client(Duration::from_secs(12))?.request(method, format!("{root}{path}"));
    if let Some(secret) = c["controller_secret"].as_str().filter(|s| !s.is_empty()) {
        req = req.bearer_auth(secret);
    }
    if let Some(v) = body {
        req = req.json(v);
    }
    let response = req
        .send()
        .await
        .map_err(|_| ApiError::bad("无法连接 Clash 控制器，请检查地址和网络"))?;
    if matches!(response.status().as_u16(), 401 | 403) {
        return Err(ApiError::bad("Clash 控制器拒绝访问，请检查密钥"));
    }
    if !response.status().is_success() {
        return Err(ApiError::bad("Clash 控制器请求失败"));
    }
    if response.status().as_u16() == 204 {
        return Ok(json!({}));
    }
    let data = bounded(response, 4 * 1024 * 1024).await?;
    if data.is_empty() {
        Ok(json!({}))
    } else {
        serde_json::from_slice(&data).map_err(|_| ApiError::bad("Clash 控制器响应格式无效"))
    }
}
async fn groups(c: &Value) -> Result<Vec<Value>> {
    let response = controller(c, Method::GET, "/proxies", None).await?;
    let proxies = response["proxies"]
        .as_object()
        .ok_or_else(|| ApiError::bad("Clash 控制器未返回有效节点列表"))?;
    let mut groups = vec![];
    for (name, p) in proxies {
        if p["type"] != "Selector" {
            continue;
        }
        if let Some(all) = p["all"].as_array() {
            let mut nodes = vec![];
            for name in all.iter().filter_map(Value::as_str) {
                let p = proxies.get(name).cloned().unwrap_or(Value::Null);
                let delay = p["history"]
                    .as_array()
                    .and_then(|v| v.last())
                    .map(|v| v["delay"].clone())
                    .filter(Value::is_number)
                    .unwrap_or(Value::Null);
                nodes.push(json!({"name":name,"type":p["type"].as_str().unwrap_or("Unknown"),"alive":p["alive"].as_bool(),"delay":delay}));
            }
            groups.push(json!({"name":name,"type":"Selector","now":p["now"].as_str().unwrap_or(""),"nodes":nodes}));
        }
    }
    Ok(groups)
}
pub async fn clash_groups(State(s): State<AppState>) -> Result<Json<Value>> {
    Ok(Json(
        json!({"groups":groups(&proxy_config(&s).await?).await?}),
    ))
}
#[derive(Deserialize)]
pub struct Select {
    group: String,
    node: String,
}
pub async fn clash_select(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<Select>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    if matches!(v.group.as_str(), "." | "..") || v.group.len() > 2048 || v.node.len() > 2048 {
        return Err(ApiError::bad("不支持此策略组名称"));
    }
    let c = proxy_config(&s).await?;
    let groups = groups(&c).await?;
    if !groups.iter().any(|g| {
        g["name"] == v.group
            && g["nodes"]
                .as_array()
                .is_some_and(|n| n.iter().any(|n| n["name"] == v.node))
    }) {
        return Err(ApiError::bad(
            "节点不在此可手动选择的策略组中，请刷新节点列表",
        ));
    }
    let encoded = url::form_urlencoded::byte_serialize(v.group.as_bytes())
        .collect::<String>()
        .replace('+', "%20");
    controller(
        &c,
        Method::PUT,
        &format!("/proxies/{encoded}"),
        Some(&json!({"name":v.node})),
    )
    .await?;
    Ok(Json(json!({"ok":true,"group":v.group,"node":v.node})))
}
#[derive(Deserialize)]
pub struct Test {
    platform: String,
}
pub async fn test_proxy(
    State(s): State<AppState>,
    ApiJson(v): ApiJson<Test>,
) -> Result<Json<Value>> {
    platform(&v.platform)?;
    let start = Instant::now();
    let proxy = outbound_proxy(&s).await?;
    let targets = HashMap::from([
        ("pixiv", "https://www.pixiv.net/"),
        ("ehentai", "https://e-hentai.org/"),
        ("picacg", "https://picaapi.picacomic.com/"),
        ("jm", "https://18comic.vip/"),
        ("kemono", "https://kemono.cr/"),
    ]);
    let config = secrets::get(&s, &format!("platform_endpoint:{}", v.platform)).await?;
    let target = if config.is_some_and(|v| v["enabled"] == true) {
        endpoint(&s, &v.platform).await?
    } else {
        targets[v.platform.as_str()].to_owned()
    };
    let response = content_client(&s).await?.get(target).send().await;
    let code = response.ok().map(|v| v.status().as_u16());
    let reachable = code.is_some_and(|n| n != 407);
    Ok(Json(
        json!({"ok":reachable&&code.is_some_and(|n|n<400),"reachable":reachable,"status_code":code,"elapsed_ms":start.elapsed().as_millis(),"message":if reachable{"已收到平台响应；此测试不验证账号权限"}else{"连接失败，请检查代理地址、端口和节点状态"},"via_proxy":proxy.is_some()}),
    ))
}
