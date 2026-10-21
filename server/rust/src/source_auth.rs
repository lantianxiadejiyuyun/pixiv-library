use crate::{
    ApiJson, AppState, accounts, auth,
    error::{ApiError, Result},
    network, platforms,
    tasks::TaskError,
    transport::{RequestBody, SafeHttp},
};
use axum::{
    Json,
    extract::{ConnectInfo, Extension, Path, State},
    http::{HeaderMap, header},
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use reqwest::Method;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    net::SocketAddr,
    sync::{Mutex, OnceLock},
    time::{Duration, Instant},
};
use subtle::ConstantTimeEq;
fn api_error(e: TaskError) -> ApiError {
    match e {
        TaskError::Failed(e) => e,
        _ => ApiError::bad("平台登录已中断，请重新发起"),
    }
}
fn name(value: &str, max: usize) -> Result<&str> {
    let value = value.trim();
    if value.is_empty() || value.chars().count() > max {
        return Err(ApiError::invalid("名称或账号长度不符合要求"));
    }
    Ok(value)
}
#[derive(Deserialize)]
pub struct Login {
    label: String,
    username: String,
    password: String,
}
pub async fn login(
    State(s): State<AppState>,
    Path(platform): Path<String>,
    ApiJson(v): ApiJson<Login>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let label = name(&v.label, 191)?;
    let username = name(&v.username, 320)?;
    if v.password.is_empty() || v.password.chars().count() > 2048 {
        return Err(ApiError::invalid("密码长度不符合要求"));
    }
    let credentials = match platform.as_str() {
        "picacg" => {
            let p = platforms::picacg::Picacg::connect(
                &s,
                &json!({"email":username,"password":v.password}),
                None,
            )
            .await
            .map_err(api_error)?;
            let user = p
                .request("users/profile", Method::GET, None)
                .await
                .map_err(api_error)?;
            if user["user"]["_id"]
                .as_str()
                .is_none_or(|s| s.is_empty() || s.len() > 128)
            {
                return Err(ApiError::bad("哔咔登录会话验证失败，未保存账号"));
            }
            p.credentials()
        }
        "jm" => login_jm(&s, username, &v.password).await?,
        "ehentai" => login_ehentai(&s, username, &v.password).await?,
        "pixiv" => {
            return Err(ApiError::bad(
                "Pixiv 使用官方 OAuth 授权，请点击官方授权登录",
            ));
        }
        _ => return Err(ApiError::bad("此平台不支持账号密码登录")),
    };
    accounts::save(&s, &platform, label, &credentials, true)
        .await
        .map(Json)
}
async fn login_jm(s: &AppState, user: &str, password: &str) -> Result<Value> {
    let root = network::endpoint(s, "jm").await?;
    let (http, jar) = SafeHttp::login(s, vec![], Some("jm"))
        .await
        .map_err(api_error)?;
    let mut h = reqwest::header::HeaderMap::new();
    h.insert(
        "origin",
        root.parse().map_err(|_| ApiError::bad("JM 登录地址无效"))?,
    );
    h.insert("referer", format!("{root}/login").parse().unwrap());
    h.insert("x-requested-with", "XMLHttpRequest".parse().unwrap());
    let response = http
        .exchange(
            Method::POST,
            &format!("{root}/login"),
            h,
            RequestBody::Form(vec![
                ("username".into(), user.into()),
                ("password".into(), password.into()),
                ("id_remember".into(), "on".into()),
                ("login_remember".into(), "on".into()),
                ("submit_login".into(), "".into()),
            ]),
        )
        .await
        .map_err(api_error)?;
    if response.status() != 200 {
        return Err(ApiError::bad("JM 登录返回意外跳转，请先完成站点验证"));
    }
    let bytes = network::bounded(response, 2 * 1024 * 1024).await?;
    let v: Value = serde_json::from_slice(&bytes)
        .map_err(|_| ApiError::bad("JM 登录响应无效，可能需要验证码"))?;
    let cookies = cookies(&jar, &root)?;
    let session = cookies
        .get("AVS")
        .filter(|v| !v.is_empty() && v.len() <= 8192)
        .ok_or_else(|| ApiError::bad("JM 未返回有效 AVS 会话，未保存账号"))?;
    if v["status"] != 1 {
        return Err(ApiError::bad("JM 登录失败，请检查账号密码或验证码"));
    }
    Ok(json!({"AVS":session}))
}
fn cookies(jar: &reqwest::cookie::Jar, url: &str) -> Result<HashMap<String, String>> {
    use reqwest::cookie::CookieStore;
    let u = url::Url::parse(url).map_err(|_| ApiError::bad("登录 Cookie 地址无效"))?;
    Ok(jar
        .cookies(&u)
        .and_then(|h| h.to_str().ok().map(str::to_owned))
        .unwrap_or_default()
        .split(';')
        .filter_map(|s| s.trim().split_once('=').map(|(k, v)| (k.into(), v.into())))
        .collect())
}
fn challenge(html: &str) -> bool {
    regex::Regex::new(r#"(?i)class=["'][^"']*(?:g-recaptcha|cf-turnstile)|<title>\s*just a moment|challenge-platform"#).unwrap().is_match(html)
}
async fn login_ehentai(s: &AppState, user: &str, password: &str) -> Result<Value> {
    let (http, jar) = SafeHttp::login(
        s,
        vec!["forums.e-hentai.org".into(), "e-hentai.org".into()],
        None,
    )
    .await
    .map_err(api_error)?;
    let root = "https://forums.e-hentai.org";
    let page = format!("{root}/index.php?act=Login&CODE=00");
    let post = format!("{root}/index.php?act=Login&CODE=01");
    let raw = http
        .bytes(
            Method::GET,
            &page,
            reqwest::header::HeaderMap::new(),
            RequestBody::Empty,
            2 * 1024 * 1024,
        )
        .await
        .map_err(api_error)?;
    let html = String::from_utf8_lossy(&raw);
    if challenge(&html) {
        return Err(ApiError::bad(
            "E-Hentai 需要浏览器或验证码验证，请完成验证后导入会话",
        ));
    }
    let mut form = HashMap::new();
    {
        let doc = scraper::Html::parse_document(&html);
        for field in doc.select(&scraper::Selector::parse("input[type=hidden][name]").unwrap()) {
            let name = field.value().attr("name").unwrap();
            let value = field.value().attr("value").unwrap_or("");
            if name.len() < 100 && value.len() < 4096 && form.len() < 50 {
                form.insert(name.to_owned(), value.to_owned());
            }
        }
    }
    for (k, v) in [
        ("UserName", user),
        ("PassWord", password),
        ("submit", "Log me in"),
        ("CookieDate", "1"),
        ("temporary_https", "off"),
    ] {
        form.insert(k.into(), v.into());
    }
    let mut h = reqwest::header::HeaderMap::new();
    h.insert("origin", root.parse().unwrap());
    h.insert("referer", page.parse().unwrap());
    let mut response = http
        .exchange(
            Method::POST,
            &post,
            h,
            RequestBody::Form(form.into_iter().collect()),
        )
        .await
        .map_err(api_error)?;
    let mut base = url::Url::parse(&post).unwrap();
    for _ in 0..3 {
        if !response.status().is_redirection() {
            break;
        }
        let location = response
            .headers()
            .get(reqwest::header::LOCATION)
            .and_then(|v| v.to_str().ok())
            .ok_or_else(|| ApiError::bad("E-Hentai 登录跳转地址缺失"))?;
        let next = base
            .join(location)
            .map_err(|_| ApiError::bad("E-Hentai 登录跳转无效"))?;
        if !["forums.e-hentai.org", "e-hentai.org"].contains(&next.host_str().unwrap_or("")) {
            return Err(ApiError::bad("E-Hentai 登录跳转超出允许站点"));
        }
        response = http
            .exchange(
                Method::GET,
                next.as_str(),
                reqwest::header::HeaderMap::new(),
                RequestBody::Empty,
            )
            .await
            .map_err(api_error)?;
        base = next;
    }
    if response.status() != 200 {
        return Err(ApiError::bad("E-Hentai 登录返回意外跳转"));
    }
    let bytes = network::bounded(response, 2 * 1024 * 1024).await?;
    let html = String::from_utf8_lossy(&bytes);
    if challenge(&html)
        || !regex::Regex::new(r"(?i)You are now logged in as:|act=Login(?:&amp;|&)CODE=03")
            .unwrap()
            .is_match(&html)
    {
        return Err(ApiError::bad(
            "E-Hentai 未确认登录成功，请检查账号密码或验证码",
        ));
    }
    let mut c = cookies(&jar, root)?;
    c.extend(cookies(&jar, "https://e-hentai.org")?);
    let member = c
        .get("ipb_member_id")
        .filter(|s| platforms::positive(s).is_ok())
        .ok_or_else(|| ApiError::bad("E-Hentai 未返回有效会员会话"))?;
    let hash = c
        .get("ipb_pass_hash")
        .filter(|s| s.len() == 32 && s.bytes().all(|b| b.is_ascii_hexdigit()))
        .ok_or_else(|| ApiError::bad("E-Hentai 未返回有效登录会话"))?;
    Ok(json!({"ipb_member_id":member,"ipb_pass_hash":hash}))
}
struct Pending {
    binding: String,
    verifier: String,
    expires: Instant,
}
static SESSIONS: OnceLock<Mutex<HashMap<String, Pending>>> = OnceLock::new();
fn binding(h: &HeaderMap) -> Result<String> {
    let cookie = h
        .get(header::COOKIE)
        .and_then(|v| v.to_str().ok())
        .and_then(|c| {
            c.split(';')
                .find_map(|p| p.trim().strip_prefix(&format!("{}=", auth::COOKIE)))
        })
        .filter(|s| !s.is_empty() && s.len() <= 2048)
        .ok_or_else(|| ApiError::bad("书库登录会话无效，请重新登录"))?;
    Ok(auth::digest(cookie))
}
fn rate(s: &AppState, action: &str, binding: &str) -> Result<()> {
    s.rates.take(
        format!("oauth:{action}:global"),
        if action == "start" { 60 } else { 120 },
        Duration::from_secs(300),
    )?;
    s.rates.take(
        format!("oauth:{action}:{binding}"),
        if action == "start" { 5 } else { 12 },
        Duration::from_secs(300),
    )
}
pub async fn start(State(s): State<AppState>, h: HeaderMap) -> Result<Json<Value>> {
    s.config.writable()?;
    let binding = binding(&h)?;
    rate(&s, "start", &binding)?;
    let mut pending = SESSIONS
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| ApiError::bad("OAuth 会话存储不可用"))?;
    pending.retain(|_, p| p.expires > Instant::now());
    if pending.len() >= 64 || pending.values().filter(|p| p.binding == binding).count() >= 3 {
        return Err(ApiError(
            axum::http::StatusCode::TOO_MANY_REQUESTS,
            "待完成的 Pixiv 授权过多，请完成已有授权或十分钟后重试",
        ));
    }
    let session = auth::random_token("", 32);
    let verifier = auth::random_token("", 64);
    let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
    pending.insert(
        session.clone(),
        Pending {
            binding,
            verifier,
            expires: Instant::now() + Duration::from_secs(600),
        },
    );
    let mut url = url::Url::parse("https://app-api.pixiv.net/web/v1/login").unwrap();
    url.query_pairs_mut().extend_pairs([
        ("code_challenge", challenge.as_str()),
        ("code_challenge_method", "S256"),
        ("client", "pixiv-android"),
    ]);
    Ok(Json(
        json!({"session_id":session,"authorization_url":url.to_string(),"expires_in":600}),
    ))
}
pub fn extract_code(value: &str) -> Result<String> {
    let error = || ApiError::bad("无法识别授权码，请粘贴 Pixiv 官方回调链接或 code 值");
    let value = value.trim();
    if value.is_empty() || value.len() > 8192 || value.chars().any(char::is_control) {
        return Err(error());
    }
    let query = if value.contains("://") {
        let u = url::Url::parse(value).map_err(|_| error())?;
        let valid =
            u.scheme() == "pixiv" && u.host_str() == Some("account") && u.path() == "/login"
                || u.scheme() == "https"
                    && u.host_str() == Some("app-api.pixiv.net")
                    && u.path() == "/web/v1/users/auth/pixiv/callback";
        if !valid
            || !u.username().is_empty()
            || u.password().is_some()
            || u.port().is_some()
            || u.fragment().is_some()
        {
            return Err(error());
        }
        Some(u.query().unwrap_or("").to_owned())
    } else if value.starts_with("code=") {
        Some(value.into())
    } else {
        None
    };
    let code = if let Some(q) = query {
        let pairs = url::form_urlencoded::parse(q.as_bytes()).collect::<Vec<_>>();
        if pairs.len() > 12 {
            return Err(error());
        }
        let codes = pairs
            .iter()
            .filter(|(k, _)| k == "code")
            .collect::<Vec<_>>();
        if codes.len() != 1 {
            return Err(error());
        }
        codes[0].1.to_string()
    } else {
        value.to_owned()
    };
    if code.len() > 2048
        || code.is_empty()
        || !code
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._~-".contains(&b))
    {
        return Err(error());
    }
    Ok(code)
}
#[derive(Deserialize)]
pub struct Finish {
    session_id: String,
    code: String,
    label: String,
}
pub async fn finish(
    State(s): State<AppState>,
    h: HeaderMap,
    ApiJson(v): ApiJson<Finish>,
) -> Result<Json<Value>> {
    s.config.writable()?;
    let label = name(&v.label, 191)?.to_owned();
    let binding = binding(&h)?;
    rate(&s, "finish", &binding)?;
    let code = extract_code(&v.code)?;
    let verifier = {
        let mut pending = SESSIONS
            .get_or_init(Default::default)
            .lock()
            .map_err(|_| ApiError::bad("OAuth 会话不可用"))?;
        let current = pending
            .get(&v.session_id)
            .filter(|p| {
                p.expires > Instant::now()
                    && bool::from(p.binding.as_bytes().ct_eq(binding.as_bytes()))
            })
            .ok_or_else(|| ApiError::bad("Pixiv 授权会话无效、过期或已使用，请重新发起"))?;
        let verifier = current.verifier.clone();
        pending.remove(&v.session_id);
        verifier
    };
    let response = platforms::pixiv::oauth(
        &s,
        vec![
            ("grant_type".into(), "authorization_code".into()),
            ("code".into(), code),
            ("code_verifier".into(), verifier),
            (
                "redirect_uri".into(),
                "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback".into(),
            ),
        ],
        None,
    )
    .await
    .map_err(api_error)?;
    let refresh = response["refresh_token"]
        .as_str()
        .filter(|v| !v.is_empty() && v.len() <= 8192 && !v.chars().any(char::is_whitespace))
        .ok_or_else(|| ApiError::bad("Pixiv 授权未返回有效 refresh_token"))?;
    let user = platforms::id(&response["user"]["id"])?;
    accounts::save(
        &s,
        "pixiv",
        &label,
        &json!({"refresh_token":refresh,"user_id":user}),
        true,
    )
    .await
    .map(Json)
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Direct {
    admin_password: String,
}
pub async fn direct(
    State(s): State<AppState>,
    connection: Option<Extension<ConnectInfo<SocketAddr>>>,
    ApiJson(v): ApiJson<Direct>,
) -> Result<Json<Value>> {
    if v.admin_password.len() > 1024 {
        return Err(ApiError::invalid("密码长度无效"));
    }
    s.rates
        .password(
            format!(
                "password:{}",
                connection
                    .map(|c| c.0.0.ip().to_string())
                    .unwrap_or_else(|| "local".into())
            ),
            &v.admin_password,
            &s.config.password,
        )
        .map_err(|e| {
            if e.0 == axum::http::StatusCode::UNAUTHORIZED {
                ApiError(
                    axum::http::StatusCode::FORBIDDEN,
                    "管理员密码错误，未导入 Pixiv 令牌",
                )
            } else {
                e
            }
        })?;
    let owner = accounts::resolve(&s, None, "metadata", false, false)
        .await?
        .ok_or_else(|| ApiError::bad("请先配置 Pixiv 主账号"))?;
    let c = accounts::credentials(&s, "pixiv", Some(&owner)).await?;
    let refresh = c["refresh_token"]
        .as_str()
        .filter(|v| {
            regex::Regex::new(r"^[A-Za-z0-9_.-]{10,4096}$")
                .unwrap()
                .is_match(v)
        })
        .ok_or_else(|| ApiError::bad("主账号令牌无效，请重新授权"))?;
    let row =
        s.db.one(
            "SELECT label FROM source_accounts WHERE id=?",
            &[json!(owner)],
        )
        .await?
        .ok_or_else(|| ApiError::bad("主账号已移除"))?;
    Ok(Json(
        json!({"account_id":owner,"account_label":row["label"],"refresh_token":refresh}),
    ))
}
