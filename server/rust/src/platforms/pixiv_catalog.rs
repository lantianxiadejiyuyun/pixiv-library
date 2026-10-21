use super::{
    id,
    pixiv::{Pixiv, metadata},
    plain, positive, str_field,
};
use crate::{
    content_store::Work,
    error::{ApiError, Result},
    network,
    tasks::TaskResult,
};
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashSet};
#[derive(Clone)]
pub struct Target {
    pub id: String,
    pub section: String,
    pub category: String,
    pub restrict: String,
}
impl Target {
    pub fn value(&self) -> Value {
        json!({"remote_id":self.id,"section":self.section,"category":self.category,"restrict":self.restrict})
    }
}
pub fn target(input: &str) -> Result<Target> {
    let input = input.trim();
    let mut t = Target {
        id: input.into(),
        section: "works".into(),
        category: "illustration".into(),
        restrict: "public".into(),
    };
    if input == "me" || positive(input).is_ok() {
        return Ok(t);
    }
    let u = network::validate_url(input, &["pixiv.net".into()])?;
    if !["pixiv.net", "www.pixiv.net"].contains(&u.host_str().unwrap_or(""))
        || u.fragment().is_some()
    {
        return Err(ApiError::bad("请输入 Pixiv 作者主页或收藏页链接"));
    }
    let re=regex::Regex::new(r"^/(?:[a-z]{2}(?:-[A-Za-z]{2,8})?/)?users/([1-9]\d{0,19})(?:/(artworks|illustrations|manga|novels|bookmarks/(?:artworks|novels)))?/?$").unwrap();
    let c = re
        .captures(u.path())
        .ok_or_else(|| ApiError::bad("请输入 Pixiv 作者主页、作品或收藏页链接"))?;
    t.id = c[1].into();
    let suffix = c.get(2).map(|c| c.as_str()).unwrap_or("");
    if suffix.starts_with("bookmarks/") {
        t.section = "bookmarks".into();
    }
    if suffix.ends_with("novels") {
        t.category = "novel".into();
    } else if suffix == "manga" {
        t.category = "manga".into();
    }
    let mut seen = HashSet::new();
    for (key, value) in u.query_pairs() {
        if !seen.insert(key.to_string()) {
            return Err(ApiError::bad("作者链接包含重复查询参数"));
        }
        match key.as_ref() {
            "rest" if value == "hide" => t.restrict = "private".into(),
            "rest" if value == "show" => (),
            "mode" if value == "manga" => t.category = "manga".into(),
            "mode" if value == "all" || value == "illust" => (),
            _ => return Err(ApiError::bad("作者链接参数不受支持")),
        }
    }
    Ok(t)
}
pub struct Page {
    pub works: Vec<Work>,
    pub next: Option<Value>,
    pub unavailable: u64,
    pub raw_count: usize,
}
pub fn cursor(
    input: &Value,
    path: &str,
    fixed: &[(&str, &str)],
    keys: &[&str],
) -> Result<Option<Value>> {
    if input.is_null() {
        return Ok(None);
    }
    let input = input
        .as_str()
        .ok_or_else(|| ApiError::bad("Pixiv 分页链接无效"))?;
    let u = network::validate_url(input, &["app-api.pixiv.net".into()])?;
    if u.host_str() != Some("app-api.pixiv.net") || u.path() != path || u.fragment().is_some() {
        return Err(ApiError::bad("Pixiv 分页链接越界，已停止读取"));
    }
    let mut params = BTreeMap::new();
    for (k, v) in u.query_pairs() {
        if params.insert(k.to_string(), v.to_string()).is_some() {
            return Err(ApiError::bad("Pixiv 分页参数重复"));
        }
    }
    if params.keys().any(|k| {
        !fixed.iter().any(|(name, _)| *name == k) && !keys.contains(&k.as_str()) && k != "filter"
    }) || params.get("filter").is_some_and(|v| v != "for_ios")
    {
        return Err(ApiError::bad("Pixiv 分页参数越界"));
    }
    for (key, value) in fixed {
        if params.get(*key).is_some_and(|v| v != value)
            || key.ends_with("_id") && !params.contains_key(*key)
        {
            return Err(ApiError::bad("Pixiv 分页身份或目录范围不一致"));
        }
    }
    let mut next = json!({});
    for key in keys {
        if let Some(value) = params.get(*key) {
            positive(value)?;
            next[*key] = json!(value);
        }
    }
    if next.as_object().unwrap().len() != 1 {
        return Err(ApiError::bad("Pixiv 分页游标无效"));
    }
    Ok(Some(next))
}
impl Pixiv {
    pub async fn profile(&self, target: &str) -> TaskResult<Value> {
        let ident = if target == "me" { self.user() } else { target };
        positive(ident)?;
        let value = self
            .request(
                "/v1/user/detail",
                &[("user_id", ident.into()), ("filter", "for_ios".into())],
            )
            .await?;
        if id(&value["user"]["id"])? != ident || str_field(&value["user"], "name").trim().is_empty()
        {
            return Err(ApiError::bad("Pixiv 作者资料格式不完整").into());
        }
        let mut stats = json!({});
        for key in [
            "total_illusts",
            "total_manga",
            "total_novels",
            "total_illust_bookmarks",
            "total_novel_bookmarks",
        ] {
            if let Some(n) = value["profile"][key].as_u64() {
                stats[key] = json!(n);
            }
        }
        Ok(
            json!({"remote_id":ident,"name":value["user"]["name"],"description":plain(str_field(&value["user"],"comment")),"statistics":stats,"avatar_url":value["user"]["profile_image_urls"]["medium"],"is_self":ident==self.user()}),
        )
    }
    pub async fn author_page(
        &self,
        ident: &str,
        section: &str,
        category: &str,
        restrict: &str,
        next: Option<&Value>,
    ) -> TaskResult<Page> {
        positive(ident)?;
        if !["works", "bookmarks"].contains(&section)
            || !["illustration", "manga", "novel"].contains(&category)
            || !["public", "private"].contains(&restrict)
            || section == "works" && restrict != "public"
        {
            return Err(ApiError::bad("Pixiv 作者目录类型无效").into());
        }
        if restrict == "private" && ident != self.user() {
            return Err(ApiError::bad("只能读取当前登录账号自己的私密收藏").into());
        }
        let novel = category == "novel";
        let path = if section == "bookmarks" {
            if novel {
                "/v1/user/bookmarks/novel"
            } else {
                "/v1/user/bookmarks/illust"
            }
        } else if novel {
            "/v1/user/novels"
        } else {
            "/v1/user/illusts"
        };
        let key = if section == "bookmarks" {
            "max_bookmark_id"
        } else {
            "offset"
        };
        let mut params = vec![("user_id", ident.into()), ("filter", "for_ios".into())];
        let mut fixed = vec![("user_id", ident)];
        let kind = if category == "manga" {
            "manga"
        } else {
            "illust"
        };
        if section == "bookmarks" {
            params.push(("restrict", restrict.into()));
            fixed.push(("restrict", restrict));
        } else if !novel {
            params.push(("type", kind.into()));
            fixed.push(("type", kind));
        }
        if let Some(next) = next {
            let map = next
                .as_object()
                .ok_or_else(|| ApiError::bad("作者分页游标无效"))?;
            if map.len() != 1 || !map.contains_key(key) {
                return Err(ApiError::bad("作者分页游标无效").into());
            }
            let value = super::id(&map[key])?;
            params.push((key, value));
        }
        let value = self.request(path, &params).await?;
        let entries = value[if novel { "novels" } else { "illusts" }]
            .as_array()
            .filter(|e| e.len() <= 500)
            .ok_or_else(|| ApiError::bad("Pixiv 作者作品列表格式无效"))?;
        let mut works = vec![];
        let mut unavailable = 0;
        let mut seen = HashSet::new();
        for e in entries {
            if e["visible"] == false || e["type"] == "ugoira" {
                unavailable += 1;
                continue;
            }
            let w = metadata(e, novel)?;
            if !seen.insert(w.remote_id.clone()) {
                return Err(ApiError::bad("Pixiv 作者目录包含重复作品").into());
            }
            if section == "works" && w.author_id.as_deref() != Some(ident) {
                return Err(ApiError::bad("Pixiv 作者目录混入其他作者作品").into());
            }
            if w.kind == category {
                works.push(w);
            } else if section == "works" {
                return Err(ApiError::bad("Pixiv 作者目录作品类型不一致").into());
            }
        }
        let cursor = cursor(&value["next_url"], path, &fixed, &[key])?;
        if cursor.is_some() && cursor.as_ref() == next {
            return Err(ApiError::bad("Pixiv 作者分页游标重复").into());
        }
        Ok(Page {
            works,
            next: cursor,
            unavailable,
            raw_count: entries.len(),
        })
    }
    pub async fn follows(&self) -> TaskResult<Vec<(String, String)>> {
        let mut results = vec![];
        let mut seen_authors = HashSet::new();
        for restrict in ["public", "private"] {
            let mut offset: Option<String> = None;
            let mut seen = HashSet::new();
            for page in 0..100 {
                let mut params = vec![
                    ("user_id", self.user().into()),
                    ("restrict", restrict.into()),
                    ("filter", "for_ios".into()),
                ];
                if let Some(offset) = &offset {
                    params.push(("offset", offset.clone()));
                }
                let value = self.request("/v1/user/following", &params).await?;
                let entries = value["user_previews"]
                    .as_array()
                    .filter(|v| v.len() <= 500)
                    .ok_or_else(|| ApiError::bad("Pixiv 关注列表格式无效"))?;
                for entry in entries {
                    let id = id(&entry["user"]["id"])?;
                    let label = str_field(&entry["user"], "name");
                    if seen_authors.insert(id.clone()) {
                        results.push((id, label.chars().take(191).collect()));
                    }
                }
                let next = cursor(
                    &value["next_url"],
                    "/v1/user/following",
                    &[("user_id", self.user()), ("restrict", restrict)],
                    &["offset"],
                )?;
                let Some(next) = next else {
                    break;
                };
                let value = next["offset"].as_str().unwrap().to_owned();
                if !seen.insert(value.clone()) || page == 99 {
                    return Err(
                        ApiError::bad("Pixiv 关注分页重复或超过 100 页，未标记同步完成").into(),
                    );
                }
                offset = Some(value);
            }
        }
        Ok(results)
    }
    pub async fn discover(
        &self,
        kind: &str,
        target: &str,
        scope: &str,
    ) -> TaskResult<Vec<Discovery>> {
        if !["images", "novels", "all"].contains(&scope)
            || !["author", "tag", "search"].contains(&kind)
        {
            return Err(ApiError::bad("Pixiv 订阅类型或内容范围无效").into());
        }
        if kind == "author" {
            positive(target)?;
        }
        let mut requests = vec![];
        if kind == "author" {
            for category in ["illustration", "manga", "novel"] {
                if category == "novel" && scope == "images"
                    || category != "novel" && scope == "novels"
                {
                    continue;
                }
                for section in ["works", "bookmarks"] {
                    if category == "manga" && section == "bookmarks" {
                        continue;
                    }
                    let novel = category == "novel";
                    let path = if section == "bookmarks" {
                        if novel {
                            "/v1/user/bookmarks/novel"
                        } else {
                            "/v1/user/bookmarks/illust"
                        }
                    } else if novel {
                        "/v1/user/novels"
                    } else {
                        "/v1/user/illusts"
                    };
                    let mut p = vec![("user_id", target.into()), ("filter", "for_ios".into())];
                    if section == "bookmarks" {
                        p.push(("restrict", "public".into()));
                    } else if !novel {
                        p.push((
                            "type",
                            if category == "manga" {
                                "manga"
                            } else {
                                "illust"
                            }
                            .into(),
                        ));
                    }
                    requests.push((path, p, novel, section));
                }
            }
        } else {
            for novel in [false, true] {
                if novel && scope == "images" || !novel && scope == "novels" {
                    continue;
                }
                requests.push((
                    if novel {
                        "/v1/search/novel"
                    } else {
                        "/v1/search/illust"
                    },
                    vec![
                        ("word", target.into()),
                        (
                            "search_target",
                            if kind == "tag" {
                                "exact_match_for_tags"
                            } else if novel {
                                "keyword"
                            } else {
                                "title_and_caption"
                            }
                            .into(),
                        ),
                        ("sort", "date_desc".into()),
                        ("filter", "for_ios".into()),
                    ],
                    novel,
                    "works",
                ));
            }
        }
        let mut out: Vec<Discovery> = vec![];
        for (path, params, novel, origin) in requests {
            let value = self.request(path, &params).await?;
            let entries = value[if novel { "novels" } else { "illusts" }]
                .as_array()
                .filter(|v| v.len() <= 500)
                .ok_or_else(|| ApiError::bad("Pixiv 作品列表格式无效"))?;
            for e in entries {
                if e["visible"] == false || e["type"] == "ugoira" {
                    continue;
                }
                let ident = format!("{}{}", if novel { "novel:" } else { "" }, id(&e["id"])?);
                if kind == "author" && origin == "works" && id(&e["user"]["id"])? != target {
                    return Err(ApiError::bad("作者作品列表混入其他作者").into());
                }
                if let Some(d) = out.iter_mut().find(|d| d.id == ident) {
                    d.origins.insert(origin.into());
                    d.has_more |= !value["next_url"].is_null();
                } else {
                    out.push(Discovery {
                        id: ident,
                        origins: HashSet::from([origin.into()]),
                        bookmarked: e["is_bookmarked"].as_bool(),
                        has_more: !value["next_url"].is_null(),
                    });
                }
            }
        }
        Ok(out)
    }
}
pub struct Discovery {
    pub id: String,
    pub origins: HashSet<String>,
    pub bookmarked: Option<bool>,
    pub has_more: bool,
}
