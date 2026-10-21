use super::{
    id,
    pixiv::{Pixiv, metadata},
    positive, str_field,
};
use crate::{
    content_store::Work,
    error::{ApiError, Result},
    network,
    tasks::TaskResult,
};
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashSet};
pub fn normalize(value: &str, kind: Option<&str>) -> Result<(String, String)> {
    let value = value.trim();
    if kind.is_some_and(|k| !["novel", "manga"].contains(&k)) {
        return Err(ApiError::bad("系列类型必须是小说或漫画"));
    }
    if positive(value).is_ok() {
        return Ok((
            value.into(),
            kind.ok_or_else(|| ApiError::bad("数字系列 ID 需要选择小说或漫画类型"))?
                .into(),
        ));
    }
    let u = network::validate_url(value, &["pixiv.net".into()])?;
    if !["pixiv.net", "www.pixiv.net"].contains(&u.host_str().unwrap_or(""))
        || u.query().is_some()
        || u.fragment().is_some()
    {
        return Err(ApiError::bad("请输入不带查询参数的 Pixiv 官方系列链接"));
    }
    let n =
        regex::Regex::new(r"^/(?:[a-z]{2}(?:-[A-Za-z]{2,8})?/)?novel/series/([1-9]\d{0,19})/?$")
            .unwrap();
    let m = regex::Regex::new(
        r"^/(?:[a-z]{2}(?:-[A-Za-z]{2,8})?/)?user/[1-9]\d{0,19}/series/([1-9]\d{0,19})/?$",
    )
    .unwrap();
    let (id, k) = if let Some(c) = n.captures(u.path()) {
        (c[1].to_owned(), "novel")
    } else if let Some(c) = m.captures(u.path()) {
        (c[1].to_owned(), "manga")
    } else {
        return Err(ApiError::bad("请输入 Pixiv 小说系列或漫画系列链接"));
    };
    if kind.is_some_and(|kind| kind != k) {
        return Err(ApiError::bad("系列链接与选择的类型不一致"));
    }
    Ok((id, k.into()))
}
pub struct Catalog {
    pub id: String,
    pub kind: String,
    pub title: String,
    pub works: Vec<Work>,
    pub complete: bool,
}
impl Pixiv {
    pub async fn series(&self, ident: &str, kind: &str) -> TaskResult<Catalog> {
        let (ident, kind) = normalize(ident, Some(kind))?;
        let novel = kind == "novel";
        let path = if novel {
            "/v2/novel/series"
        } else {
            "/v1/illust/series"
        };
        let field = if novel {
            "series_id"
        } else {
            "illust_series_id"
        };
        let mut works = vec![];
        let mut seen = HashSet::new();
        let mut cursors = HashSet::new();
        let mut params = vec![(field, ident.clone()), ("filter", "for_ios".into())];
        let mut title = String::new();
        let mut unavailable = false;
        let mut complete = false;
        for _ in 0..50 {
            let v = self.request(path, &params).await?;
            let detail = &v[if novel {
                "novel_series_detail"
            } else {
                "illust_series_detail"
            }];
            let name = str_field(detail, "title");
            let entries = v[if novel { "novels" } else { "illusts" }]
                .as_array()
                .filter(|e| e.len() <= 1000)
                .ok_or_else(|| ApiError::bad("Pixiv 系列作品目录格式无效"))?;
            if name.trim().is_empty()
                || name.chars().count() > 512
                || v.get("next_url").is_none()
                || !detail["id"].is_null() && id(&detail["id"])? != ident
            {
                return Err(ApiError::bad("Pixiv 系列目录不完整或编号不一致").into());
            }
            if title.is_empty() {
                title = name.into();
            }
            for e in entries {
                if e["visible"] == false {
                    unavailable = true;
                    continue;
                }
                let mut w = metadata(e, novel)?;
                if let Some(series) = &w.series
                    && series["remote_id"] != ident
                {
                    return Err(ApiError::bad("Pixiv 系列目录混入其他系列的作品").into());
                }
                w.series = Some(json!({"remote_id":ident,"title":title,"kind":kind}));
                if seen.insert(w.remote_id.clone()) {
                    works.push(w);
                }
                if works.len() >= 1000 {
                    break;
                }
            }
            let next = super::pixiv_catalog::cursor(
                &v["next_url"],
                path,
                &[(field, &ident)],
                if novel {
                    &["last_order"]
                } else {
                    &["last_order", "offset"]
                },
            )?;
            if next.is_none() {
                complete = !unavailable;
                break;
            }
            let next = next.unwrap();
            let marker = next.to_string();
            if !cursors.insert(marker) {
                return Err(ApiError::bad("Pixiv 系列分页出现循环，未保存不完整目录").into());
            }
            if works.len() >= 1000 {
                break;
            }
            params = vec![(field, ident.clone()), ("filter", "for_ios".into())];
            for key in ["last_order", "offset"] {
                if let Some(value) = next[key].as_str() {
                    params.push((key, value.into()));
                }
            }
        }
        Ok(Catalog {
            id: ident,
            kind,
            title,
            works,
            complete,
        })
    }
    pub async fn watchlist(&self) -> TaskResult<Vec<Value>> {
        let mut out = vec![];
        for kind in ["novel", "manga"] {
            let path = format!("/v1/watchlist/{kind}");
            let mut params = vec![("filter", "for_ios".into())];
            let mut seen = HashSet::new();
            let mut offsets = HashSet::new();
            for page in 0..51 {
                let v = self.request(&path, &params).await?;
                let entries = v["series"]
                    .as_array()
                    .filter(|e| e.len() <= 100)
                    .ok_or_else(|| ApiError::bad("Pixiv 追更列表格式不完整，原列表已保留"))?;
                if v.get("next_url").is_none() {
                    return Err(ApiError::bad("Pixiv 追更分页信息缺失").into());
                }
                for e in entries {
                    let ident = id(&e["id"])?;
                    if !seen.insert(ident.clone())
                        || seen.len() > if kind == "novel" { 1000 } else { 500 }
                    {
                        return Err(ApiError::bad("Pixiv 追更系列重复或超过同步容量").into());
                    }
                    let title = str_field(e, "title");
                    let author = e["user"]["name"]
                        .as_str()
                        .ok_or_else(|| ApiError::bad("追更作者资料无效"))?;
                    let count = e["published_content_count"]
                        .as_u64()
                        .filter(|n| *n <= 1_000_000)
                        .ok_or_else(|| ApiError::bad("追更系列篇数无效"))?;
                    if title.trim().is_empty() || title.chars().count() > 512 {
                        return Err(ApiError::bad("追更系列标题无效").into());
                    }
                    let stamp = &e["last_published_content_datetime"];
                    if !stamp.is_null()
                        && !stamp
                            .as_str()
                            .is_some_and(|s| s.len() <= 64 && super::pixiv::timestamp(s).is_some())
                    {
                        return Err(ApiError::bad("追更系列更新时间无效").into());
                    }
                    let latest = if e["latest_content_id"].is_null() {
                        Value::Null
                    } else {
                        json!(id(&e["latest_content_id"])?)
                    };
                    out.push(json!({"remote_id":ident,"kind":kind,"title":title,"author":author.chars().take(191).collect::<String>(),"author_id":id(&e["user"]["id"])? ,"content_count":count,"latest_content_id":latest,"remote_updated_at":stamp}));
                }
                if v["next_url"].is_null() {
                    break;
                }
                if page == 50 {
                    return Err(ApiError::bad("Pixiv 追更分页超过限制，原列表已保留").into());
                }
                let next = watch_cursor(&v["next_url"], &path)?;
                let offset = next["offset"].as_str().unwrap().to_owned();
                if !offsets.insert(offset.clone()) {
                    return Err(ApiError::bad("Pixiv 追更分页出现重复游标").into());
                }
                params = vec![
                    ("filter", "for_ios".into()),
                    ("offset", offset),
                    ("limit", next["limit"].as_str().unwrap().into()),
                ];
            }
        }
        Ok(out)
    }
}
fn watch_cursor(v: &Value, path: &str) -> Result<Value> {
    let raw = v
        .as_str()
        .ok_or_else(|| ApiError::bad("追更分页链接无效"))?;
    let u = network::validate_url(raw, &["app-api.pixiv.net".into()])?;
    if u.host_str() != Some("app-api.pixiv.net") || u.path() != path || u.fragment().is_some() {
        return Err(ApiError::bad("追更分页链接越界"));
    }
    let mut p = BTreeMap::new();
    for (k, v) in u.query_pairs() {
        if !["offset", "limit", "filter"].contains(&k.as_ref())
            || p.insert(k.to_string(), v.to_string()).is_some()
        {
            return Err(ApiError::bad("追更分页参数无效"));
        }
    }
    let offset = p
        .get("offset")
        .filter(|s| !s.is_empty() && s.len() <= 8 && s.bytes().all(|b| b.is_ascii_digit()))
        .ok_or_else(|| ApiError::bad("追更分页偏移无效"))?;
    let limit = p.get("limit").map(String::as_str).unwrap_or("30");
    if !limit.parse::<u32>().is_ok_and(|n| (1..=100).contains(&n))
        || p.get("filter").is_some_and(|f| f != "for_ios")
    {
        return Err(ApiError::bad("追更分页范围无效"));
    }
    Ok(json!({"offset":offset,"limit":limit}))
}
