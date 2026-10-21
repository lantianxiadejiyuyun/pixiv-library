pub mod accounts;
pub mod acquisition;
mod ai_bundle;
pub mod ai_nodes;
pub mod app_updates;
mod auth;
pub mod authors;
mod catalog;
pub mod characters;
pub mod config;
pub mod content_store;
mod cover_scheduler;
pub mod database_export;
pub mod db;
mod devices;
pub mod error;
pub mod executor;
mod favorites;
pub mod item_api;
mod library;
pub mod managed_proxy;
mod media;
pub mod migrations;
mod mobile_admin;
pub mod network;
pub mod performance;
pub mod platforms;
pub mod proxy_subscription;
mod queue;
pub mod runtime;
pub mod secrets;
mod series;
pub mod series_sync;
pub mod source_auth;
pub mod speed;
pub mod subscriptions;
mod task_api;
pub mod tasks;
pub mod translation;
pub mod transport;
pub mod watchlist;

use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, FromRequest, Request},
    http::StatusCode,
    middleware,
    response::IntoResponse,
    routing::{delete, get, post, put},
};
use config::Config;
use db::Db;
use error::{ApiError, Result};
use serde::de::DeserializeOwned;
use std::sync::Arc;
use tokio::sync::Semaphore;
use tower_http::{catch_panic::CatchPanicLayer, services::ServeDir, trace::TraceLayer};

pub struct Inner {
    pub config: Config,
    pub db: Db,
    pub fs_limit: Arc<Semaphore>,
    pub thumbnail_limit: Arc<Semaphore>,
    pub thumbnail_cache: tempfile::TempDir,
    pub cpu_limit: Arc<Semaphore>,
    pub rates: auth::Rates,
    pub runtime: std::sync::RwLock<serde_json::Value>,
    pub telemetry: std::sync::RwLock<serde_json::Value>,
    pub import_limit: Arc<Semaphore>,
    pub export_limit: Arc<Semaphore>,
    pub apk_limit: Arc<Semaphore>,
    pub backup_limit: Arc<Semaphore>,
    pub secret_cache: secrets::Cache,
    pub proxy_core: tokio::sync::Mutex<managed_proxy::Core>,
    pub proxy_status: std::sync::RwLock<serde_json::Value>,
    pub proxy_probe: Arc<Semaphore>,
    pub proxy_batches: tokio::sync::Mutex<std::collections::VecDeque<serde_json::Value>>,
}
pub type AppState = Arc<Inner>;
impl Inner {
    pub async fn connect(config: Config) -> Result<AppState> {
        config.validate()?;
        let db = Db::connect(&config).await?;
        if !config.production && !config.require_marker && !config.read_only {
            use tokio::io::AsyncWriteExt;
            tokio::fs::create_dir_all(&config.storage_root).await?;
            match tokio::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(config.storage_root.join(".library-storage"))
                .await
            {
                Ok(mut file) => file.write_all(b"qiye-library-v1\n").await?,
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => (),
                Err(e) => return Err(e.into()),
            }
        }
        let thumbnail_cache = tempfile::Builder::new()
            .prefix("qiye-rust-thumbnails-")
            .tempdir()?;
        Ok(Arc::new(Self {
            config,
            db,
            fs_limit: Arc::new(Semaphore::new(4)),
            thumbnail_limit: Arc::new(Semaphore::new(1)),
            thumbnail_cache,
            cpu_limit: Arc::new(Semaphore::new(2)),
            rates: Default::default(),
            telemetry: std::sync::RwLock::new(
                serde_json::json!({"status":"sampling","refresh_seconds":2,"sample_age_seconds":null,"current":null,"history":[],"database":null,"storage":[],"storage_stale":true}),
            ),
            import_limit: Arc::new(Semaphore::new(1)),
            export_limit: Arc::new(Semaphore::new(1)),
            apk_limit: Arc::new(Semaphore::new(1)),
            backup_limit: Arc::new(Semaphore::new(1)),
            secret_cache: Default::default(),
            proxy_core: Default::default(),
            proxy_status: std::sync::RwLock::new(
                serde_json::json!({"installed":false,"running":false,"state":"stopped","error":null,"version":null,"restart_count":0,"node_count":0,"selected":null,"proxy_url":"http://127.0.0.1:17890"}),
            ),
            proxy_probe: Arc::new(Semaphore::new(6)),
            proxy_batches: Default::default(),
            runtime: std::sync::RwLock::new(
                serde_json::json!({"mode":"disabled","workers":[],"workers_migrated":true}),
            ),
        }))
    }
}
pub async fn connect(config: Config) -> Result<AppState> {
    Inner::connect(config).await
}

pub struct ApiJson<T>(pub T);
impl<S: Send + Sync, T: DeserializeOwned> FromRequest<S> for ApiJson<T> {
    type Rejection = ApiError;
    async fn from_request(req: Request, state: &S) -> Result<Self> {
        Json::<T>::from_request(req, state)
            .await
            .map(|Json(v)| Self(v))
            .map_err(|_| ApiError::invalid("请求内容格式或字段类型不正确"))
    }
}
pub fn router(state: AppState) -> Router {
    let frontend = state.config.frontend.clone();
    Router::new()
        .route("/api/network/managed", get(managed_proxy::status))
        .route(
            "/api/network/managed/subscriptions",
            post(managed_proxy::add),
        )
        .route(
            "/api/network/managed/subscriptions/{id}",
            axum::routing::patch(managed_proxy::patch).delete(managed_proxy::remove),
        )
        .route(
            "/api/network/managed/subscriptions/{id}/refresh",
            post(managed_proxy::refresh),
        )
        .route("/api/network/managed/enable", post(managed_proxy::enable))
        .route("/api/network/managed/select", post(managed_proxy::select))
        .route("/api/network/managed/delay", post(managed_proxy::delay))
        .route(
            "/api/network/managed/delay-batch",
            post(managed_proxy::batch).get(managed_proxy::latest),
        )
        .route(
            "/api/network/managed/delay-batch/{id}",
            get(managed_proxy::batch_status),
        )
        .route("/api/network/managed/restart", post(managed_proxy::restart))
        .route(
            "/api/characters",
            get(characters::list).post(characters::add),
        )
        .route("/api/characters/search", post(characters::search))
        .route("/api/characters/{id}", get(characters::detail))
        .route("/api/characters/{id}/profile", post(characters::update))
        .route("/api/ai/characters/search", post(characters::search))
        .route("/api/ai/characters/confirm", post(characters::confirm))
        .route(
            "/api/ai/characters/image/settings",
            get(characters::image_settings).put(characters::image_save),
        )
        .route(
            "/api/ai/characters/image-search",
            post(characters::image_search).layer(DefaultBodyLimit::max(9 * 1024 * 1024)),
        )
        .route("/api/health", get(media::health))
        .route("/api/database/export", post(database_export::export))
        .route(
            "/api/app-updates/android",
            get(app_updates::latest).post(app_updates::upload),
        )
        .route(
            "/api/app-updates/android/{id}/apk",
            get(app_updates::download),
        )
        .route("/api/network/speed/ping", get(speed::ping))
        .route("/api/network/speed/download", get(speed::download))
        .route("/api/network/speed/upload", post(speed::upload))
        .route("/api/mobile-admin/catalog", get(mobile_admin::catalog))
        .route("/api/server/performance", get(performance::get))
        .route(
            "/api/imports/upload",
            post(item_api::upload).layer(DefaultBodyLimit::max(513 * 1024 * 1024)),
        )
        .route("/api/imports/folder", post(item_api::folder))
        .route(
            "/api/items/{id}/cover-refresh",
            post(item_api::cover_refresh),
        )
        .route("/api/items/{id}/export", get(item_api::export))
        .route("/api/queue/covers", post(item_api::covers))
        .route("/api/ai/status", get(ai_nodes::status))
        .route("/api/ai/analyze", post(ai_nodes::analyze))
        .route("/api/ai/nodes", get(ai_nodes::list).post(ai_nodes::add))
        .route("/api/ai/nodes/deployment", get(ai_bundle::get))
        .route("/api/ai/nodes/routing", put(ai_nodes::routing))
        .route(
            "/api/ai/nodes/{id}",
            put(ai_nodes::update).delete(ai_nodes::remove),
        )
        .route("/api/ai/nodes/{id}/test", post(ai_nodes::relay))
        .route("/api/ai/nodes/{id}/vision", get(ai_nodes::relay))
        .route(
            "/api/ai/nodes/{id}/models",
            get(ai_nodes::relay).delete(ai_nodes::relay),
        )
        .route("/api/ai/nodes/{id}/models/pull", post(ai_nodes::relay))
        .route("/api/ai/nodes/{id}/models/unload", post(ai_nodes::relay))
        .route("/api/ai/nodes/{id}/operations", get(ai_nodes::relay))
        .route(
            "/api/ai/nodes/{id}/operations/{operation}",
            get(ai_nodes::relay),
        )
        .route(
            "/api/ai/nodes/{id}/services/{service}/{action}",
            post(ai_nodes::relay),
        )
        .route(
            "/api/ai/nodes/{id}/translation",
            post(translation::use_node),
        )
        .route(
            "/api/translation/settings",
            get(translation::settings).put(translation::save),
        )
        .route("/api/translation/status", get(translation::status))
        .route("/api/translation/test", post(translation::test))
        .route("/api/translation/batch", post(translation::batch))
        .route("/api/items/{id}/translate", post(translation::translate))
        .route("/api/items/{id}/translation", get(translation::item_status))
        .route("/api/sources/{platform}/login", post(source_auth::login))
        .route("/api/sources/pixiv/oauth/start", post(source_auth::start))
        .route("/api/sources/pixiv/oauth/finish", post(source_auth::finish))
        .route("/api/pixiv/direct/credentials", post(source_auth::direct))
        .route(
            "/api/pixiv/bookmarks/sync",
            post(acquisition::bookmark_scan),
        )
        .route("/api/series/import", post(series_sync::import))
        .route("/api/pixiv/watchlist", get(watchlist::list))
        .route("/api/pixiv/watchlist/sync", post(watchlist::sync))
        .route("/api/pixiv/watchlist/settings", put(watchlist::settings))
        .route(
            "/api/pixiv/watchlist/{id}",
            get(watchlist::detail).put(watchlist::edit),
        )
        .route("/api/pixiv/watchlist/{id}/check", post(watchlist::check))
        .route("/api/pixiv/watchlist/{id}/read", post(watchlist::read))
        .route("/api/pixiv/authors", get(authors::list))
        .route("/api/pixiv/authors/open", post(authors::open))
        .route("/api/pixiv/authors/{id}", get(authors::detail))
        .route("/api/pixiv/authors/{id}/avatar", get(authors::avatar))
        .route(
            "/api/pixiv/authors/{id}/feed",
            get(authors::get_feed).post(authors::request_feed),
        )
        .route(
            "/api/pixiv/authors/{id}/download-settings",
            put(authors::set_downloads),
        )
        .route("/api/pixiv/tasks/{id}", get(authors::task))
        .route("/api/auth/status", get(auth::status))
        .route("/api/auth/login", post(auth::login))
        .route("/api/auth/logout", post(auth::logout))
        .route("/api/dashboard", get(library::dashboard))
        .route("/api/items", get(library::list))
        .route(
            "/api/items/{id}",
            get(library::detail)
                .patch(item_api::patch)
                .delete(item_api::remove),
        )
        .route("/api/tags", get(library::tags))
        .route("/api/library/collections", get(library::collections))
        .route("/api/settings", get(library::settings))
        .route("/api/items/{id}/cover", get(media::cover))
        .route(
            "/api/assets/{id}/content",
            get(media::content).head(media::content),
        )
        .route("/api/assets/{id}/text-sections", get(media::text_sections))
        .route("/api/assets/{id}/thumbnail", get(media::thumbnail))
        .route("/api/items/{id}/progress", put(media::progress))
        .route(
            "/api/favorite-groups",
            get(favorites::groups).post(favorites::create_group),
        )
        .route(
            "/api/favorite-groups/{id}",
            put(favorites::rename_group).delete(favorites::remove_group),
        )
        .route("/api/items/{id}/favorite", put(favorites::favorite))
        .route("/api/items/{id}/score", put(favorites::score))
        .route("/api/items/{id}/visit", post(favorites::visit))
        .route("/api/history", delete(favorites::clear_history))
        .route("/api/devices", get(devices::list))
        .route("/api/devices/{id}", delete(devices::revoke))
        .route("/api/devices/pairing", post(devices::pair))
        .route("/api/devices/pairing/exchange", post(devices::exchange))
        .route("/api/devices/disconnect", post(devices::disconnect))
        .route("/api/rust/migration", get(catalog::status))
        .route("/api/platform-endpoints", get(network::endpoints))
        .route(
            "/api/platform-endpoints/{platform}",
            put(network::set_endpoint),
        )
        .route(
            "/api/network/proxy",
            get(network::proxy).put(network::set_proxy),
        )
        .route("/api/network/clash/groups", get(network::clash_groups))
        .route("/api/network/clash/select", post(network::clash_select))
        .route("/api/network/proxy/test", post(network::test_proxy))
        .route("/api/sources/{platform}/accounts", post(accounts::add))
        .route("/api/sources", get(subscriptions::sources))
        .route(
            "/api/sources/{platform}/import",
            post(subscriptions::source_import),
        )
        .route(
            "/api/sources/{platform}/sync",
            post(subscriptions::source_sync),
        )
        .route("/api/items/{id}/download", post(subscriptions::download))
        .route(
            "/api/subscriptions",
            get(subscriptions::list).post(subscriptions::create),
        )
        .route(
            "/api/subscriptions/{id}",
            axum::routing::patch(subscriptions::patch).delete(subscriptions::remove),
        )
        .route(
            "/api/tag-tracking",
            get(subscriptions::tags).post(subscriptions::add_tags),
        )
        .route(
            "/api/tag-tracking/{id}",
            axum::routing::patch(subscriptions::tag_patch).delete(subscriptions::tag_remove),
        )
        .route(
            "/api/tag-tracking/{id}/check",
            post(subscriptions::tag_check),
        )
        .route(
            "/api/pixiv/follow-download-settings",
            get(subscriptions::follow_policy).put(subscriptions::set_follow_policy),
        )
        .route(
            "/api/pixiv/collection-settings",
            get(subscriptions::bookmark_policy).put(subscriptions::set_bookmark_policy),
        )
        .route(
            "/api/sources/{platform}/accounts/{id}",
            delete(accounts::remove),
        )
        .route(
            "/api/sources/pixiv/account-roles",
            get(accounts::get_roles).put(accounts::set_roles),
        )
        .route(
            "/api/queue/settings",
            get(queue::settings).put(queue::update_settings),
        )
        .route("/api/queue/lanes/{id}", put(queue::update_lane))
        .route("/api/queue/sources", get(queue::sources))
        .route(
            "/api/queue/sources/{id}",
            get(queue::source).put(queue::update_source),
        )
        .route("/api/queue/status", get(queue::status))
        .route("/api/jobs", get(queue::jobs))
        .route("/api/jobs/{id}", get(queue::job))
        .route("/api/jobs/{id}/retry", post(task_api::retry))
        .route("/api/jobs/{id}/cancel", post(task_api::cancel))
        .route("/api/series", post(series::create))
        .route("/api/series/{id}", get(series::get).put(series::update))
        .fallback_service(ServeDir::new(frontend))
        .layer(DefaultBodyLimit::max(1024 * 1024))
        .layer(middleware::from_fn_with_state(state.clone(), auth::guard))
        .layer(CatchPanicLayer::custom(|_| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"detail":"服务处理失败，请稍后重试"})),
            )
                .into_response()
        }))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}
