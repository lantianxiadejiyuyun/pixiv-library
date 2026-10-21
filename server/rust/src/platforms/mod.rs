pub mod ehentai;
pub mod jm;
pub mod kemono;
pub mod picacg;
pub mod pixiv;
pub mod pixiv_catalog;
pub mod pixiv_series;
use crate::{
    AppState,
    content_store::Work,
    error::{ApiError, Result},
    tasks::{Context, TaskResult},
    transport::{RequestBody, SafeHttp},
};
use reqwest::{
    Method,
    header::{HeaderMap, HeaderValue},
};
use serde_json::Value;
pub fn str_field<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
pub fn id(v: &Value) -> Result<String> {
    let text = if let Some(n) = v.as_u64() {
        n.to_string()
    } else {
        v.as_str().unwrap_or("").to_owned()
    };
    positive(&text)?;
    Ok(text)
}
pub fn positive(v: &str) -> Result<()> {
    if v.is_empty() || v.len() > 20 || v.starts_with('0') || !v.bytes().all(|b| b.is_ascii_digit())
    {
        Err(ApiError::bad("平台返回的编号无效"))
    } else {
        Ok(())
    }
}
pub fn plain(v: &str) -> String {
    let doc = scraper::Html::parse_fragment(v);
    doc.root_element().text().collect::<String>()
}
pub fn sensitive(value: &str) -> Result<HeaderValue> {
    let mut v = HeaderValue::from_str(value).map_err(|_| ApiError::bad("账号凭据格式无效"))?;
    v.set_sensitive(true);
    Ok(v)
}
pub fn suffix(url: &str) -> Result<String> {
    let u = url::Url::parse(url).map_err(|_| ApiError::bad("图片链接无效"))?;
    let ext = std::path::Path::new(u.path())
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if ["jpg", "jpeg", "png", "webp", "gif", "bmp"].contains(&ext.as_str()) {
        Ok(ext)
    } else {
        Err(ApiError::bad("平台返回不支持的图片格式"))
    }
}
pub async fn preview(
    s: &AppState,
    url: &str,
    hosts: &[&str],
    headers: HeaderMap,
    ctx: Option<Context>,
) -> TaskResult<Option<(String, Vec<u8>)>> {
    if url.is_empty() {
        return Ok(None);
    }
    let http = SafeHttp::new(s, hosts.iter().map(|v| v.to_string()).collect(), ctx).await?;
    match http
        .bytes(
            Method::GET,
            url,
            headers,
            RequestBody::Empty,
            8 * 1024 * 1024,
        )
        .await
    {
        Ok(b) => Ok(Some((
            format!("preview.{}", suffix(url).unwrap_or_else(|_| "jpg".into())),
            b,
        ))),
        Err(crate::tasks::TaskError::Paused) => Err(crate::tasks::TaskError::Paused),
        Err(crate::tasks::TaskError::Cancelled) => Err(crate::tasks::TaskError::Cancelled),
        Err(_) => Ok(None),
    }
}
pub fn normalize(platform: &str, input: &str) -> Result<String> {
    match platform {
        "pixiv" => pixiv::normalize(input),
        "picacg" => picacg::normalize(input),
        "kemono" => kemono::normalize(input, false),
        "ehentai" => ehentai::normalize(input),
        "jm" => jm::normalize(input),
        _ => Err(ApiError::bad("不支持的内容平台")),
    }
}
pub async fn fetch(
    s: &AppState,
    platform: &str,
    credentials: &Value,
    remote: &str,
    download: bool,
    include_preview: bool,
    ctx: Option<Context>,
) -> TaskResult<Work> {
    match platform {
        "pixiv" => {
            pixiv::Pixiv::connect(s, credentials, ctx)
                .await?
                .fetch(remote, download, include_preview)
                .await
        }
        "picacg" => {
            picacg::Picacg::connect(s, credentials, ctx)
                .await?
                .fetch(remote, download)
                .await
        }
        "kemono" => {
            kemono::Kemono::connect(s, ctx)
                .await?
                .fetch(remote, download)
                .await
        }
        "ehentai" => ehentai::fetch(s, credentials, remote, download, ctx).await,
        "jm" => jm::fetch(s, credentials, remote, download, ctx).await,
        _ => Err(ApiError::bad("不支持的内容平台").into()),
    }
}
