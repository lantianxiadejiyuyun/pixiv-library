use crate::AppState;
use axum::{Json, extract::State};
use regex::Regex;
use serde_json::{Value, json};
use std::sync::OnceLock;
pub const ROUTES: &[(&str, &str)] = &[
    ("GET", "/api/ai/nodes/deployment"),
    ("GET", "/api/network/managed"),
    ("POST", "/api/network/managed/subscriptions"),
    ("PATCH", "/api/network/managed/subscriptions/{id}"),
    ("DELETE", "/api/network/managed/subscriptions/{id}"),
    ("POST", "/api/network/managed/subscriptions/{id}/refresh"),
    ("POST", "/api/network/managed/enable"),
    ("POST", "/api/network/managed/select"),
    ("POST", "/api/network/managed/delay"),
    ("POST", "/api/network/managed/delay-batch"),
    ("GET", "/api/network/managed/delay-batch"),
    ("GET", "/api/network/managed/delay-batch/{id}"),
    ("POST", "/api/network/managed/restart"),
    ("GET", "/api/characters"),
    ("POST", "/api/characters"),
    ("POST", "/api/characters/search"),
    ("GET", "/api/characters/{id}"),
    ("POST", "/api/characters/{id}/profile"),
    ("POST", "/api/ai/characters/search"),
    ("POST", "/api/ai/characters/confirm"),
    ("GET", "/api/ai/characters/image/settings"),
    ("PUT", "/api/ai/characters/image/settings"),
    ("POST", "/api/ai/characters/image-search"),
    ("POST", "/api/database/export"),
    ("GET", "/api/app-updates/android"),
    ("POST", "/api/app-updates/android"),
    ("GET", "/api/app-updates/android/{id}/apk"),
    ("GET", "/api/network/speed/ping"),
    ("GET", "/api/network/speed/download"),
    ("POST", "/api/network/speed/upload"),
    ("GET", "/api/mobile-admin/catalog"),
    ("GET", "/api/server/performance"),
    ("POST", "/api/imports/upload"),
    ("POST", "/api/imports/folder"),
    ("PATCH", "/api/items/{id}"),
    ("DELETE", "/api/items/{id}"),
    ("POST", "/api/items/{id}/cover-refresh"),
    ("GET", "/api/items/{id}/export"),
    ("POST", "/api/queue/covers"),
    ("GET", "/api/ai/status"),
    ("POST", "/api/ai/analyze"),
    ("GET", "/api/ai/nodes"),
    ("POST", "/api/ai/nodes"),
    ("PUT", "/api/ai/nodes/routing"),
    ("PUT", "/api/ai/nodes/{id}"),
    ("DELETE", "/api/ai/nodes/{id}"),
    ("POST", "/api/ai/nodes/{id}/test"),
    ("GET", "/api/ai/nodes/{id}/vision"),
    ("GET", "/api/ai/nodes/{id}/models"),
    ("DELETE", "/api/ai/nodes/{id}/models"),
    ("POST", "/api/ai/nodes/{id}/models/pull"),
    ("POST", "/api/ai/nodes/{id}/models/unload"),
    ("GET", "/api/ai/nodes/{id}/operations"),
    ("GET", "/api/ai/nodes/{id}/operations/{operation}"),
    ("POST", "/api/ai/nodes/{id}/services/{service}/{action}"),
    ("POST", "/api/ai/nodes/{id}/translation"),
    ("GET", "/api/translation/settings"),
    ("PUT", "/api/translation/settings"),
    ("GET", "/api/translation/status"),
    ("POST", "/api/translation/test"),
    ("POST", "/api/translation/batch"),
    ("POST", "/api/items/{id}/translate"),
    ("GET", "/api/items/{id}/translation"),
    ("POST", "/api/sources/{platform}/login"),
    ("POST", "/api/sources/pixiv/oauth/start"),
    ("POST", "/api/sources/pixiv/oauth/finish"),
    ("POST", "/api/pixiv/direct/credentials"),
    ("POST", "/api/pixiv/bookmarks/sync"),
    ("POST", "/api/series/import"),
    ("GET", "/api/pixiv/watchlist"),
    ("POST", "/api/pixiv/watchlist/sync"),
    ("PUT", "/api/pixiv/watchlist/settings"),
    ("GET", "/api/pixiv/watchlist/{id}"),
    ("PUT", "/api/pixiv/watchlist/{id}"),
    ("POST", "/api/pixiv/watchlist/{id}/check"),
    ("POST", "/api/pixiv/watchlist/{id}/read"),
    ("GET", "/api/pixiv/authors"),
    ("POST", "/api/pixiv/authors/open"),
    ("GET", "/api/pixiv/authors/{id}"),
    ("GET", "/api/pixiv/authors/{id}/avatar"),
    ("GET", "/api/pixiv/authors/{id}/feed"),
    ("POST", "/api/pixiv/authors/{id}/feed"),
    ("PUT", "/api/pixiv/authors/{id}/download-settings"),
    ("GET", "/api/pixiv/tasks/{id}"),
    ("GET", "/api/sources"),
    ("POST", "/api/sources/{platform}/import"),
    ("POST", "/api/sources/{platform}/sync"),
    ("POST", "/api/items/{id}/download"),
    ("GET", "/api/subscriptions"),
    ("POST", "/api/subscriptions"),
    ("PATCH", "/api/subscriptions/{id}"),
    ("DELETE", "/api/subscriptions/{id}"),
    ("GET", "/api/tag-tracking"),
    ("POST", "/api/tag-tracking"),
    ("PATCH", "/api/tag-tracking/{id}"),
    ("DELETE", "/api/tag-tracking/{id}"),
    ("POST", "/api/tag-tracking/{id}/check"),
    ("GET", "/api/pixiv/follow-download-settings"),
    ("PUT", "/api/pixiv/follow-download-settings"),
    ("GET", "/api/pixiv/collection-settings"),
    ("PUT", "/api/pixiv/collection-settings"),
    ("POST", "/api/jobs/{id}/retry"),
    ("POST", "/api/jobs/{id}/cancel"),
    ("GET", "/api/platform-endpoints"),
    ("PUT", "/api/platform-endpoints/{platform}"),
    ("GET", "/api/network/proxy"),
    ("PUT", "/api/network/proxy"),
    ("GET", "/api/network/clash/groups"),
    ("POST", "/api/network/clash/select"),
    ("POST", "/api/network/proxy/test"),
    ("POST", "/api/sources/{platform}/accounts"),
    ("DELETE", "/api/sources/{platform}/accounts/{id}"),
    ("GET", "/api/sources/pixiv/account-roles"),
    ("PUT", "/api/sources/pixiv/account-roles"),
    ("GET", "/api/health"),
    ("GET", "/api/auth/status"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/dashboard"),
    ("GET", "/api/items"),
    ("GET", "/api/items/{id}"),
    ("GET", "/api/tags"),
    ("GET", "/api/library/collections"),
    ("GET", "/api/settings"),
    ("GET", "/api/items/{id}/cover"),
    ("GET", "/api/assets/{id}/content"),
    ("HEAD", "/api/assets/{id}/content"),
    ("GET", "/api/assets/{id}/text-sections"),
    ("PUT", "/api/items/{id}/progress"),
    ("GET", "/api/assets/{id}/thumbnail"),
    ("GET", "/api/queue/settings"),
    ("PUT", "/api/queue/settings"),
    ("PUT", "/api/queue/lanes/{id}"),
    ("GET", "/api/queue/status"),
    ("GET", "/api/jobs"),
    ("GET", "/api/jobs/{id}"),
    ("GET", "/api/series/{id}"),
    ("POST", "/api/series"),
    ("PUT", "/api/series/{id}"),
    ("GET", "/api/favorite-groups"),
    ("POST", "/api/favorite-groups"),
    ("PUT", "/api/favorite-groups/{id}"),
    ("DELETE", "/api/favorite-groups/{id}"),
    ("PUT", "/api/items/{id}/favorite"),
    ("PUT", "/api/items/{id}/score"),
    ("POST", "/api/items/{id}/visit"),
    ("DELETE", "/api/history"),
    ("GET", "/api/devices"),
    ("DELETE", "/api/devices/{id}"),
    ("POST", "/api/devices/pairing"),
    ("POST", "/api/devices/pairing/exchange"),
    ("POST", "/api/devices/disconnect"),
    ("GET", "/api/rust/migration"),
];
pub fn implemented(method: &str, path: &str) -> bool {
    static ROUTE_RE: OnceLock<Vec<(&str, Regex)>> = OnceLock::new();
    ROUTE_RE
        .get_or_init(|| ROUTES.iter().map(|(m, p)| (*m, pattern(p))).collect())
        .iter()
        .any(|(m, re)| *m == method && re.is_match(path))
}
fn pattern(template: &str) -> Regex {
    let mut rest = template;
    let mut pattern = String::from("^");
    while let Some(open) = rest.find('{') {
        pattern.push_str(&regex::escape(&rest[..open]));
        let end = rest[open..].find('}').unwrap() + open;
        pattern.push_str("[^/]+");
        rest = &rest[end + 1..];
    }
    pattern.push_str(&regex::escape(rest));
    pattern.push('$');
    Regex::new(&pattern).unwrap()
}
fn legacy_routes() -> &'static Vec<(String, Regex)> {
    static LEGACY: OnceLock<Vec<(String, Regex)>> = OnceLock::new();
    LEGACY.get_or_init(|| {
        let routes: Vec<Value> = serde_json::from_str(include_str!("../contracts/routes.json"))
            .expect("checked-in route contract");
        routes
            .into_iter()
            .map(|r| {
                (
                    r["method"].as_str().unwrap().to_owned(),
                    pattern(r["path"].as_str().unwrap()),
                )
            })
            .collect()
    })
}
pub fn unported(method: &str, path: &str) -> bool {
    legacy_routes()
        .iter()
        .any(|(m, re)| m == method && re.is_match(path))
}
pub fn known_path(path: &str) -> bool {
    legacy_routes().iter().any(|(_, re)| re.is_match(path))
        || ROUTES.iter().any(|(method, _)| implemented(method, path))
}
pub async fn status(State(s): State<AppState>) -> Json<Value> {
    let legacy: Value =
        serde_json::from_str(include_str!("../contracts/routes.json")).unwrap_or(json!([]));
    let ported: Vec<_> = ROUTES
        .iter()
        .filter(|(_, p)| *p != "/api/rust/migration")
        .map(|(m, p)| json!({"method":m,"path":p}))
        .collect();
    Json(
        json!({"runtime":"rust","production_ready":false,"read_only":s.config.read_only,"legacy_route_count":legacy.as_array().map(Vec::len),"ported_route_count":ported.len(),"ported":ported,"remaining":legacy.as_array().map(|v|v.iter().filter(|r|!implemented(r["method"].as_str().unwrap_or(""),r["example_path"].as_str().unwrap_or(""))).collect::<Vec<_>>()),"workers_migrated":true}),
    )
}
