//! Untrusted subscriptions yield only sanitized proxy nodes, never executable
//! profiles, providers, local certificate paths, DNS or routing configuration.
use crate::{
    AppState,
    error::{ApiError, Result},
    network,
};
use base64::{
    Engine,
    engine::general_purpose::{STANDARD, STANDARD_NO_PAD, URL_SAFE, URL_SAFE_NO_PAD},
};
use futures_util::StreamExt;
use serde_json::{Map, Value, json};
use std::{
    collections::{HashMap, HashSet},
    time::Duration,
};
const KEYS: &str = "name type server port password username cipher uuid alterId security udp tls servername sni alpn skip-cert-verify fingerprint client-fingerprint network ws-opts grpc-opts http-opts h2-opts reality-opts flow packet-encoding encryption plugin plugin-opts obfs obfs-param protocol protocol-param auth auth-str up down ports hop-interval recv-window-conn recv-window disable-mtu-discovery fast-open obfs-password congestion-controller udp-relay-mode reduce-rtt heartbeat-interval request-timeout version idle-session-check-interval idle-session-timeout min-idle-session udp-over-tcp udp-over-tcp-version smux tfo ip-version token disable-sni name-cert-verify";
const FORBIDDEN: &[&str] = &[
    "certificate",
    "certificate-path",
    "private-key",
    "private-key-path",
    "ca",
    "ca-str",
    "interface-name",
    "routing-mark",
    "dialer-proxy",
    "proxy-provider",
    "path-file",
];
const UNSUPPORTED: &[&str] = &[
    "ech-opts",
    "shadow-tls-opts",
    "restls-opts",
    "jls-opts",
    "tlsmirror-opts",
    "realm-opts",
];
fn bad() -> ApiError {
    ApiError::bad("订阅节点无效或暂不支持该格式，请使用常规 Clash/Mihomo 节点订阅")
}
fn tree(v: &Value, depth: usize) -> Result<()> {
    if depth > 12 {
        return Err(bad());
    }
    match v {
        Value::String(s) => {
            if s.len() > 16384
                || s.chars()
                    .any(|c| c.is_control() && !matches!(c, '\t' | '\n'))
            {
                return Err(bad());
            }
        }
        Value::Array(a) => {
            if a.len() > 200 {
                return Err(bad());
            }
            for v in a {
                tree(v, depth + 1)?;
            }
        }
        Value::Object(o) => {
            if o.len() > 100 || o.keys().any(|k| FORBIDDEN.contains(&k.as_str())) {
                return Err(bad());
            }
            for v in o.values() {
                tree(v, depth + 1)?;
            }
        }
        _ => (),
    };
    Ok(())
}
pub fn sanitize(v: Value) -> Result<Value> {
    let o = v.as_object().ok_or_else(bad)?;
    tree(&v, 0)?;
    let kind = v["type"].as_str().ok_or_else(bad)?;
    if ![
        "ss",
        "ssr",
        "vmess",
        "vless",
        "trojan",
        "hysteria",
        "hysteria2",
        "tuic",
        "socks5",
        "http",
        "anytls",
        "snell",
    ]
    .contains(&kind)
        || o.keys().any(|k| UNSUPPORTED.contains(&k.as_str()))
    {
        return Err(bad());
    }
    let name = v["name"].as_str().ok_or_else(bad)?.trim();
    let server = v["server"].as_str().ok_or_else(bad)?;
    let port = v["port"]
        .as_u64()
        .or_else(|| v["port"].as_str().and_then(|s| s.parse().ok()))
        .filter(|n| (1..=65535).contains(n))
        .ok_or_else(bad)?;
    if name.is_empty()
        || name.chars().count() > 256
        || name.chars().any(char::is_control)
        || server.is_empty()
        || server.len() > 253
        || server.contains(['/', '\\', '@', '?', '#'])
        || server.chars().any(|c| c.is_control() || c.is_whitespace())
        || !matches!(
            v["network"].as_str().unwrap_or("tcp"),
            "tcp" | "raw" | "ws" | "grpc" | "http" | "h2"
        )
        || !matches!(
            v["plugin"].as_str(),
            None | Some("obfs" | "v2ray-plugin" | "shadow-tls" | "restls")
        )
    {
        return Err(bad());
    }
    let mut out = o
        .iter()
        .filter(|(k, _)| KEYS.split_whitespace().any(|key| key == k.as_str()))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect::<Map<_, _>>();
    out.insert("name".into(), json!(name));
    out.insert("port".into(), json!(port));
    if out.get("network") == Some(&json!("raw")) {
        out.insert("network".into(), json!("tcp"));
    }
    Ok(Value::Object(out))
}
fn b64(raw: &str) -> Result<String> {
    let raw = raw.trim();
    let bytes = [&STANDARD, &STANDARD_NO_PAD, &URL_SAFE, &URL_SAFE_NO_PAD]
        .into_iter()
        .find_map(|e| e.decode(raw).ok())
        .ok_or_else(bad)?;
    String::from_utf8(bytes).map_err(|_| bad())
}
fn decode(raw: &str) -> Result<String> {
    // Decode URL components without '+' changing to space.
    let mut out = vec![];
    let b = raw.as_bytes();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            if i + 2 >= b.len() {
                return Err(bad());
            }
            let h = |x: u8| match x {
                b'0'..=b'9' => Some(x - b'0'),
                b'a'..=b'f' => Some(x - b'a' + 10),
                b'A'..=b'F' => Some(x - b'A' + 10),
                _ => None,
            };
            out.push(h(b[i + 1]).ok_or_else(bad)? * 16 + h(b[i + 2]).ok_or_else(bad)?);
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    String::from_utf8(out).map_err(|_| bad())
}
fn boolean(s: &str) -> Result<bool> {
    match s.to_ascii_lowercase().as_str() {
        "true" | "1" => Ok(true),
        "false" | "0" => Ok(false),
        _ => Err(bad()),
    }
}
fn transport(node: &mut Value, q: &HashMap<String, String>) -> Result<()> {
    let get = |k: &str, f: &str| q.get(k).map(String::as_str).unwrap_or(f).to_owned();
    let kind = get("type", &get("network", "tcp"));
    match kind.as_str() {
        "tcp" | "raw" | "none" | "" => (),
        "ws" => {
            node["network"] = json!("ws");
            node["ws-opts"] = json!({"path":get("path","/")});
            if q.contains_key("host") {
                node["ws-opts"]["headers"] = json!({"Host":get("host","")});
            }
        }
        "grpc" => {
            node["network"] = json!("grpc");
            node["grpc-opts"] = json!({"grpc-service-name":get("serviceName","")});
        }
        "h2" => {
            node["network"] = json!("h2");
            node["h2-opts"] = json!({"path":get("path","/"),"host":[get("host",node["server"].as_str().unwrap_or(""))]});
        }
        "http" => {
            node["network"] = json!("http");
            node["http-opts"] = json!({"path":[get("path","/")]});
            if q.contains_key("host") {
                node["http-opts"]["headers"] = json!({"Host":[get("host","")]});
            }
        }
        _ => return Err(bad()),
    }
    Ok(())
}
pub fn uri(raw: &str) -> Result<Value> {
    if raw.len() > 65536 {
        return Err(bad());
    }
    if let Some(raw) = raw.strip_prefix("vmess://") {
        let v: Value = serde_json::from_str(&b64(raw)?).map_err(|_| bad())?;
        let mut n = json!({"name":v["ps"].as_str().filter(|s|!s.is_empty()).unwrap_or("VMess"),"type":"vmess","server":v["add"],"port":v["port"],"uuid":v["id"],"alterId":v["aid"].as_u64().or_else(||v["aid"].as_str().and_then(|s|s.parse().ok())).unwrap_or(0),"cipher":v["scy"].as_str().unwrap_or("auto"),"tls":v["tls"]=="tls","udp":true});
        for (a, b) in [("sni", "servername"), ("fp", "client-fingerprint")] {
            if let Some(s) = v[a].as_str().filter(|s| !s.is_empty()) {
                n[b] = json!(s);
            }
        }
        if let Some(s) = v["alpn"].as_str() {
            n["alpn"] = json!(s.split(',').collect::<Vec<_>>());
        }
        let net = if v["type"] == "http" {
            "http"
        } else if v["net"] == "http" {
            "h2"
        } else {
            v["net"].as_str().unwrap_or("tcp")
        };
        let q = HashMap::from([
            ("type".into(), net.into()),
            ("host".into(), v["host"].as_str().unwrap_or("").into()),
            ("path".into(), v["path"].as_str().unwrap_or("/").into()),
            (
                "serviceName".into(),
                v["path"].as_str().unwrap_or("").into(),
            ),
        ]);
        transport(&mut n, &q)?;
        return sanitize(n);
    }
    let url = url::Url::parse(raw).map_err(|_| bad())?;
    let pairs = url.query_pairs().collect::<Vec<_>>();
    if pairs.len() > 40 {
        return Err(bad());
    }
    let mut q = HashMap::new();
    for (k, v) in pairs {
        if q.insert(k.to_string(), v.to_string()).is_some() {
            return Err(bad());
        }
    }
    if q.keys().any(|k| {
        FORBIDDEN.contains(&k.as_str())
            || k.to_lowercase().contains("ech")
            || UNSUPPORTED.contains(&k.as_str())
    }) {
        return Err(bad());
    }
    let get = |k: &str| q.get(k).map(String::as_str).unwrap_or("");
    let name = decode(url.fragment().unwrap_or(""))?;
    let name = if name.is_empty() {
        url.scheme().to_uppercase()
    } else {
        name
    };
    if url.scheme() == "ss" {
        let authority = raw[5..]
            .split(['?', '#'])
            .next()
            .unwrap_or("")
            .trim_end_matches('/');
        let authority = if authority.contains('@') {
            authority.to_owned()
        } else {
            b64(authority)?
        };
        let (auth, endpoint) = authority.rsplit_once('@').ok_or_else(bad)?;
        let auth = decode(auth)?;
        let auth = if auth.contains(':') {
            auth
        } else {
            b64(&auth)?
        };
        let (cipher, password) = auth.split_once(':').ok_or_else(bad)?;
        let endpoint = url::Url::parse(&format!("ss://{endpoint}")).map_err(|_| bad())?;
        let mut n = json!({"name":name,"type":"ss","server":endpoint.host_str(),"port":endpoint.port(),"cipher":cipher,"password":password,"udp":true});
        if !get("plugin").is_empty() {
            let mut p = get("plugin").split(';');
            let plugin = p.next().unwrap_or("");
            let plugin = if matches!(plugin, "obfs-local" | "simple-obfs") {
                "obfs"
            } else {
                plugin
            };
            let mut opts = Map::new();
            for part in p.filter(|s| !s.is_empty()) {
                let (k, v) = part
                    .split_once('=')
                    .map(|(k, v)| (k, json!(v)))
                    .unwrap_or((part, json!(true)));
                opts.insert(k.into(), v);
            }
            n["plugin"] = json!(plugin);
            n["plugin-opts"] = if plugin == "obfs" {
                json!({"mode":opts.get("obfs").cloned().unwrap_or(json!("http")),"host":opts.get("obfs-host").cloned().unwrap_or(json!(""))})
            } else {
                json!(opts)
            };
        }
        return sanitize(n);
    }
    let kind = if url.scheme() == "hy2" {
        "hysteria2"
    } else {
        url.scheme()
    };
    if !["vless", "trojan", "hysteria2", "tuic"].contains(&kind) {
        return Err(bad());
    }
    let mut n = json!({"name":name,"type":kind,"server":url.host_str(),"port":url.port().unwrap_or(443),"udp":true});
    let auth = decode(url.username())?;
    if kind == "tuic" && url.password().is_none() {
        n["token"] = json!(auth);
    } else if matches!(kind, "vless" | "tuic") {
        n["uuid"] = json!(auth);
        if kind == "tuic" {
            n["password"] = json!(decode(url.password().unwrap_or(""))?);
        }
    } else {
        n["password"] = json!(if let Some(p) = url.password() {
            format!("{auth}:{}", decode(p)?)
        } else {
            auth
        });
    }
    let sni = if !get("sni").is_empty() {
        get("sni")
    } else {
        get("peer")
    };
    if !sni.is_empty() {
        n[if kind == "vless" { "servername" } else { "sni" }] = json!(sni);
    }
    if !get("alpn").is_empty() {
        n["alpn"] = json!(get("alpn").split(',').collect::<Vec<_>>());
    }
    for key in ["insecure", "allowInsecure", "allow_insecure"] {
        if q.contains_key(key) {
            n["skip-cert-verify"] = json!(boolean(get(key))?);
            break;
        }
    }
    let pin = if !get("pinSHA256").is_empty() {
        get("pinSHA256")
    } else {
        get("pcs")
    };
    if !pin.is_empty() {
        n["fingerprint"] = json!(pin);
    }
    if matches!(kind, "vless" | "trojan") && !get("fp").is_empty() {
        n["client-fingerprint"] = json!(get("fp"));
    }
    if kind == "vless" {
        let sec = if get("security").is_empty() {
            "none"
        } else {
            get("security")
        };
        if !matches!(sec, "none" | "tls" | "reality") {
            return Err(bad());
        }
        n["tls"] = json!(sec != "none");
        for key in ["encryption", "flow"] {
            if !get(key).is_empty() {
                n[key] = json!(get(key));
            }
        }
        if sec == "reality" {
            n["reality-opts"] = json!({"public-key":get("pbk"),"short-id":get("sid")});
            n["client-fingerprint"] = json!(if get("fp").is_empty() {
                "chrome"
            } else {
                get("fp")
            });
        }
    }
    if kind == "trojan" && !matches!(get("security"), "" | "tls") {
        return Err(bad());
    }
    if matches!(kind, "vless" | "trojan") {
        let mut q = q.clone();
        if kind == "vless" && get("type") == "http" {
            q.insert("type".into(), "h2".into());
        }
        transport(&mut n, &q)?;
    }
    if kind == "hysteria2" {
        for key in ["obfs", "obfs-password", "up", "down"] {
            if !get(key).is_empty() {
                n[key] = json!(get(key));
            }
        }
    }
    if kind == "tuic" {
        for (a, b) in [
            ("congestion_control", "congestion-controller"),
            ("udp_relay_mode", "udp-relay-mode"),
        ] {
            if !get(a).is_empty() {
                n[b] = json!(get(a));
            }
        }
        for (a, b) in [("disable_sni", "disable-sni"), ("reduce_rtt", "reduce-rtt")] {
            if q.contains_key(a) {
                n[b] = json!(boolean(get(a))?);
            }
        }
    }
    sanitize(n)
}
fn yaml_preflight(text: &str) -> Result<()> {
    // Disallow all aliases/anchors before serde_yaml can expand any.
    let (mut quote, mut escaped, mut depth, mut count) = (None, false, 0usize, 0usize);
    let mut indents = vec![];
    for line in text.lines() {
        let trim = line.trim_start();
        if trim.is_empty() || trim.starts_with('#') {
            continue;
        }
        let indent = line.len() - trim.len();
        while indents.last().is_some_and(|v| *v >= indent) {
            indents.pop();
        }
        indents.push(indent);
        if indents.len() > 16 {
            return Err(bad());
        }
        for c in line.chars() {
            count += 1;
            if count > 2 * 1024 * 1024 {
                return Err(bad());
            }
            if escaped {
                escaped = false;
                continue;
            }
            if let Some(q) = quote {
                if c == '\\' && q == '"' {
                    escaped = true;
                } else if c == q {
                    quote = None;
                }
                continue;
            }
            match c {
                '#' => break,
                '\'' | '"' => quote = Some(c),
                '&' | '*' => return Err(bad()),
                '{' | '[' => {
                    depth += 1;
                    if depth > 16 {
                        return Err(bad());
                    }
                }
                '}' | ']' => depth = depth.saturating_sub(1),
                _ => (),
            }
        }
    }
    Ok(())
}
pub fn parse(data: &[u8]) -> Result<Vec<Value>> {
    if data.is_empty() || data.len() > 2 * 1024 * 1024 {
        return Err(bad());
    }
    let text = std::str::from_utf8(data)
        .map_err(|_| bad())?
        .trim_start_matches('\u{feff}')
        .trim();
    let raw = if text.contains("proxies:") || text.starts_with('{') {
        yaml_preflight(text)?;
        let v: Value = serde_yaml::from_str(text).map_err(|_| bad())?;
        v["proxies"].as_array().cloned().ok_or_else(bad)?
    } else {
        let text = if text.contains("://") {
            text.into()
        } else {
            b64(&text
                .chars()
                .filter(|c| !c.is_whitespace())
                .collect::<String>())?
        };
        let lines = text
            .lines()
            .map(str::trim)
            .filter(|l| !l.is_empty() && !l.starts_with('#'))
            .collect::<Vec<_>>();
        if lines.len() > 1000 {
            return Err(bad());
        }
        lines.into_iter().map(uri).collect::<Result<Vec<_>>>()?
    };
    if raw.is_empty() || raw.len() > 1000 {
        return Err(bad());
    }
    let nodes = raw.into_iter().map(sanitize).collect::<Result<Vec<_>>>()?;
    let mut names = HashSet::new();
    if nodes
        .iter()
        .any(|v| !names.insert(v["name"].as_str().unwrap_or("")))
    {
        return Err(ApiError::bad("订阅存在重复节点名称"));
    }
    Ok(nodes)
}
pub fn validate_url(raw: &str) -> Result<url::Url> {
    let u = url::Url::parse(raw).map_err(|_| bad())?;
    let h = u.host_str().ok_or_else(bad)?;
    if raw.len() > 4096
        || raw.chars().any(|c| c.is_whitespace() || c.is_control())
        || raw.contains('\\')
        || u.scheme() != "https"
        || !u.username().is_empty()
        || u.password().is_some()
        || u.port().is_some_and(|p| p != 443)
        || u.fragment().is_some()
        || !h.contains('.')
        || [
            ".localhost",
            ".local",
            ".lan",
            ".internal",
            ".home",
            ".localdomain",
        ]
        .iter()
        .any(|s| h.ends_with(s))
        || h.parse().is_ok_and(|ip| !network::public_ip(ip))
    {
        return Err(ApiError::bad("订阅须为公网 HTTPS 链接（443 端口）"));
    }
    Ok(u)
}
pub async fn fetch(s: &AppState, raw: &str, via_proxy: bool) -> Result<Vec<Value>> {
    let mut u = validate_url(raw)?;
    for _ in 0..5 {
        let host = u.host_str().ok_or_else(bad)?;
        let addresses = addresses(host).await?;
        let client = if via_proxy {
            network::content_client(s).await?
        } else {
            reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(Duration::from_secs(30))
                .connect_timeout(Duration::from_secs(8))
                .resolve_to_addrs(host, &addresses)
                .build()
                .map_err(|_| bad())?
        };
        let response = client
            .get(u.clone())
            .header("Accept", "application/yaml,text/plain,*/*")
            .timeout(Duration::from_secs(30))
            .send()
            .await
            .map_err(|_| ApiError::bad("订阅获取失败，请检查地址、网络与代理"))?;
        if response.status().is_redirection() {
            let next = response
                .headers()
                .get("location")
                .and_then(|v| v.to_str().ok())
                .ok_or_else(bad)?;
            u = validate_url(u.join(next).map_err(|_| bad())?.as_str())?;
            continue;
        }
        if !response.status().is_success() {
            return Err(ApiError::bad("订阅服务器暂不可用"));
        }
        let mut bytes = vec![];
        let mut stream = response.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| bad())?;
            if bytes.len() + chunk.len() > 2 * 1024 * 1024 {
                return Err(ApiError::bad("订阅超过 2 MiB 限制"));
            }
            bytes.extend_from_slice(&chunk);
        }
        return parse(&bytes);
    }
    Err(ApiError::bad("订阅重定向过多"))
}
fn fake_address(ip: std::net::IpAddr) -> bool {
    match ip {
        std::net::IpAddr::V4(ip) => ip.octets()[0] == 198 && (18..=19).contains(&ip.octets()[1]),
        std::net::IpAddr::V6(ip) => {
            ip.segments()[0] == 0x2001 && ip.segments()[1] == 2 && ip.segments()[2] == 0
        }
    }
}
pub fn doh_answers(v: &Value, host: &str, kind: u64) -> Result<Vec<std::net::SocketAddr>> {
    if v["Status"] != 0
        || v["TC"] == true
        || !v["Question"].as_array().is_some_and(|q| {
            q.iter().any(|q| {
                q["name"]
                    .as_str()
                    .is_some_and(|n| n.trim_end_matches('.').eq_ignore_ascii_case(host))
                    && q["type"] == kind
            })
        })
    {
        return Err(bad());
    }
    let answers = v["Answer"]
        .as_array()
        .filter(|a| a.len() <= 100)
        .ok_or_else(bad)?;
    let mut allowed = HashSet::from([host.trim_end_matches('.').to_ascii_lowercase()]);
    for _ in 0..8 {
        for answer in answers {
            let owner = answer["name"]
                .as_str()
                .unwrap_or("")
                .trim_end_matches('.')
                .to_ascii_lowercase();
            if answer["type"] == 5
                && allowed.contains(&owner)
                && let Some(target) = answer["data"].as_str()
            {
                allowed.insert(target.trim_end_matches('.').to_ascii_lowercase());
            }
        }
    }
    let mut output = vec![];
    for answer in answers {
        if answer["type"] == kind
            && answer["name"]
                .as_str()
                .is_some_and(|n| allowed.contains(&n.trim_end_matches('.').to_ascii_lowercase()))
        {
            let ip = answer["data"]
                .as_str()
                .ok_or_else(bad)?
                .parse::<std::net::IpAddr>()
                .map_err(|_| bad())?;
            if !network::public_ip(ip) || (kind == 1) != ip.is_ipv4() {
                return Err(bad());
            }
            output.push(std::net::SocketAddr::new(ip, 443));
        }
    }
    Ok(output)
}
async fn addresses(host: &str) -> Result<Vec<std::net::SocketAddr>> {
    let system = tokio::time::timeout(Duration::from_secs(5), tokio::net::lookup_host((host, 443)))
        .await
        .ok()
        .and_then(|r| r.ok())
        .map(|v| v.collect::<Vec<_>>())
        .unwrap_or_default();
    if !system.is_empty() && system.iter().all(|a| network::public_ip(a.ip())) {
        return Ok(system);
    }
    if system
        .iter()
        .any(|a| !network::public_ip(a.ip()) && !fake_address(a.ip()))
    {
        return Err(bad());
    }
    // Only failed/fake-IP system DNS may fall back to these fixed HTTPS DNS
    // services. Subscription paths and tokens are never included in queries.
    for (host_doh, ip, path) in [
        ("dns.alidns.com", "223.5.5.5", "/resolve"),
        ("cloudflare-dns.com", "1.1.1.1", "/dns-query"),
    ] {
        let client = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(5))
            .resolve(
                host_doh,
                std::net::SocketAddr::new(ip.parse().map_err(|_| bad())?, 443),
            )
            .build()
            .map_err(|_| bad())?;
        let mut resolved = vec![];
        for kind in [1, 28] {
            let r = client
                .get(format!("https://{host_doh}{path}"))
                .query(&[("name", host), ("type", &kind.to_string())])
                .header("Accept", "application/dns-json")
                .send()
                .await;
            let Ok(r) = r else { continue };
            if !r.status().is_success() {
                continue;
            }
            if let Ok(bytes) = network::bounded(r, 65536).await
                && let Ok(data) = serde_json::from_slice::<Value>(&bytes)
                && let Ok(found) = doh_answers(&data, host, kind)
            {
                resolved.extend(found);
            }
        }
        if !resolved.is_empty() {
            return Ok(resolved);
        }
    }
    Err(ApiError::bad("无法解析订阅公网域名，请检查 DNS 或代理"))
}
