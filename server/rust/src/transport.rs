//! Platform-only HTTP. AI/controller LAN traffic uses a separate direct client.
use crate::{
    AppState,
    error::ApiError,
    network,
    tasks::{Context, TaskError, TaskResult},
};
use reqwest::{
    Client, Method,
    header::{AUTHORIZATION, COOKIE, HeaderMap, LOCATION},
};
use serde_json::Value;
#[derive(Clone)]
pub enum RequestBody {
    Empty,
    Json(Value),
    Form(Vec<(String, String)>),
}
#[derive(Clone)]
pub struct SafeHttp {
    client: Client,
    hosts: Vec<String>,
    root: Option<(String, bool)>,
    ctx: Option<Context>,
    proxy: bool,
}
impl SafeHttp {
    pub async fn login(
        s: &AppState,
        hosts: Vec<String>,
        platform: Option<&str>,
    ) -> TaskResult<(Self, std::sync::Arc<reqwest::cookie::Jar>)> {
        let jar = std::sync::Arc::new(reqwest::cookie::Jar::default());
        let mut http = Self::new(s, hosts, None).await?;
        http.client = network::content_client_cookies(s, Some(jar.clone())).await?;
        if let Some(platform) = platform {
            let root = network::endpoint(s, platform).await?;
            http.hosts = vec![
                url::Url::parse(&root)
                    .map_err(|_| ApiError::bad("平台登录地址无效"))?
                    .host_str()
                    .unwrap()
                    .into(),
            ];
            http.root = Some((root, false));
        }
        Ok((http, jar))
    }
    /// One response, including a redirect. The login protocol must validate and
    /// follow each GET explicitly; passwords are never replayed to another URL.
    pub async fn exchange(
        &self,
        method: Method,
        target: &str,
        headers: HeaderMap,
        body: RequestBody,
    ) -> TaskResult<reqwest::Response> {
        let url = self.check(target).await?;
        let mut r = self.client.request(method, url).headers(headers);
        r = match body {
            RequestBody::Empty => r,
            RequestBody::Form(v) => r.form(&v),
            RequestBody::Json(v) => r.json(&v),
        };
        let response = r
            .send()
            .await
            .map_err(|_| ApiError::bad("无法连接平台登录服务，请检查网络"))?;
        if response.status().as_u16() >= 400 {
            return Err(ApiError::bad("平台未接受登录请求，请检查账号密码或验证码要求").into());
        }
        Ok(response)
    }
    pub async fn new(s: &AppState, hosts: Vec<String>, ctx: Option<Context>) -> TaskResult<Self> {
        let proxy = network::outbound_proxy(s).await?.is_some();
        Ok(Self {
            client: network::content_client(s).await?,
            hosts,
            root: None,
            ctx,
            proxy,
        })
    }
    pub async fn api(
        s: &AppState,
        platform: &str,
        ctx: Option<Context>,
    ) -> TaskResult<(Self, String)> {
        let root = network::endpoint(s, platform).await?;
        let url = url::Url::parse(&root).map_err(|_| ApiError::bad("平台接口配置无效"))?;
        let host = url
            .host_str()
            .ok_or_else(|| ApiError::bad("平台接口配置无效"))?
            .to_owned();
        let mut http = Self::new(s, vec![host], ctx).await?;
        http.root = Some((root.clone(), platform == "ehentai"));
        Ok((http, root))
    }
    async fn check(&self, target: &str) -> TaskResult<url::Url> {
        let u = network::validate_url(target, &self.hosts)?;
        if let Some((root, exact)) = &self.root {
            let root = url::Url::parse(root).map_err(|_| ApiError::bad("平台接口配置无效"))?;
            let prefix = root.path().trim_end_matches('/');
            let raw = u.path().to_ascii_lowercase();
            if u.host_str() != root.host_str()
                || u.fragment().is_some()
                || ["%2e", "%2f", "%5c", "%25"]
                    .iter()
                    .any(|part| raw.contains(part))
                || *exact && u.path() != root.path()
                || !*exact
                    && !prefix.is_empty()
                    && u.path() != prefix
                    && !u.path().starts_with(&format!("{prefix}/"))
            {
                return Err(ApiError::bad("平台请求或重定向超出已配置的接口地址，已停止").into());
            }
            // Custom API origins are checked even with a remote-DNS proxy.
            if !self.proxy
                || !network::ENDPOINTS
                    .iter()
                    .any(|(_, _, url, _)| *url == root.as_str().trim_end_matches('/'))
            {
                network::public_dns(root.host_str().unwrap()).await?;
            }
        } else if !self.proxy {
            network::public_dns(u.host_str().unwrap()).await?;
        }
        Ok(u)
    }
    pub async fn send(
        &self,
        method: Method,
        target: &str,
        headers: HeaderMap,
        body: RequestBody,
    ) -> TaskResult<reqwest::Response> {
        let mut target = target.to_owned();
        for _ in 0..4 {
            let u = self.check(&target).await?;
            if let Some(ctx) = &self.ctx {
                ctx.request_gate().await?;
            }
            let mut req = self
                .client
                .request(method.clone(), u.clone())
                .headers(headers.clone());
            req = match &body {
                RequestBody::Empty => req,
                RequestBody::Json(v) => req.json(v),
                RequestBody::Form(v) => req.form(v),
            };
            let response=if let Some(ctx)=&self.ctx{tokio::select!{_=ctx.stop.cancelled()=>return Err(TaskError::Paused),r=req.send()=>r}}else{req.send().await}.map_err(|_|ApiError::bad("平台连接失败，请检查网络后重试"))?;
            let status = response.status().as_u16();
            if matches!(status, 301 | 302 | 303 | 307 | 308) {
                if method != Method::GET
                    || headers.contains_key(AUTHORIZATION)
                    || headers.contains_key(COOKIE)
                {
                    return Err(ApiError::bad("平台接口返回意外重定向，已停止请求").into());
                }
                let location = response
                    .headers()
                    .get(LOCATION)
                    .and_then(|v| v.to_str().ok())
                    .ok_or_else(|| ApiError::bad("平台重定向地址缺失"))?;
                target = u
                    .join(location)
                    .map_err(|_| ApiError::bad("平台重定向地址无效"))?
                    .to_string();
                continue;
            }
            if status == 429 {
                if let Some(ctx) = &self.ctx {
                    ctx.defer(
                        response
                            .headers()
                            .get("retry-after")
                            .and_then(|v| v.to_str().ok()),
                    )
                    .await?;
                }
                return Err(ApiError::bad("平台触发请求冷却，请稍后在重试队列重试").into());
            }
            if matches!(status, 401 | 403) {
                return Err(ApiError::bad("平台拒绝访问，请检查账号凭据及账号权限").into());
            }
            if status >= 400 {
                return Err(ApiError::bad("平台请求失败，作品可能不可访问或服务暂时不可用").into());
            }
            return Ok(response);
        }
        Err(ApiError::bad("平台重定向次数过多").into())
    }
    pub async fn bytes(
        &self,
        method: Method,
        url: &str,
        headers: HeaderMap,
        body: RequestBody,
        limit: usize,
    ) -> TaskResult<Vec<u8>> {
        let response = self.send(method, url, headers, body).await?;
        Ok(network::bounded(response, limit).await?)
    }
    pub async fn json(
        &self,
        method: Method,
        url: &str,
        headers: HeaderMap,
        body: RequestBody,
    ) -> TaskResult<Value> {
        let bytes = self
            .bytes(method, url, headers, body, 8 * 1024 * 1024)
            .await?;
        serde_json::from_slice(&bytes)
            .map_err(|_| ApiError::bad("平台响应不是有效 JSON，请检查网络或更新适配器").into())
    }
}
