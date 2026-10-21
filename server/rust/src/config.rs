use crate::error::{ApiError, Result};
use std::{env, net::SocketAddr, path::PathBuf, time::Duration};

#[derive(Clone)]
pub struct Config {
    pub database_url: String,
    pub storage_root: PathBuf,
    pub import_root: PathBuf,
    pub frontend: PathBuf,
    pub password: String,
    pub secret: String,
    pub production: bool,
    pub secure_cookie: bool,
    pub require_marker: bool,
    pub allowed_origins: Vec<String>,
    pub bind: SocketAddr,
    pub pool_size: u32,
    pub query_timeout: Duration,
    pub read_only: bool,
}
fn var(name: &str, fallback: &str) -> String {
    env::var(name).unwrap_or_else(|_| fallback.to_owned())
}
fn flag(name: &str, fallback: bool) -> bool {
    env::var(name)
        .map(|v| matches!(v.to_lowercase().as_str(), "true" | "1" | "yes"))
        .unwrap_or(fallback)
}
impl Config {
    pub fn from_env() -> Result<Self> {
        let config = Self {
            database_url: var(
                "LIBRARY_DATABASE_URL",
                "mysql://library:library@127.0.0.1:3306/library",
            ),
            storage_root: PathBuf::from(var("LIBRARY_STORAGE_ROOT", "./data/library")),
            import_root: PathBuf::from(var("LIBRARY_IMPORT_ROOT", "./data/import")),
            frontend: PathBuf::from(var("LIBRARY_FRONTEND_DIST", "../../web/dist")),
            password: var("LIBRARY_ADMIN_PASSWORD", "local-library"),
            secret: var(
                "LIBRARY_SECRET_KEY",
                "development-only-secret-change-before-deployment",
            ),
            production: var("LIBRARY_ENV", "development") == "production",
            secure_cookie: flag("LIBRARY_SECURE_COOKIE", false),
            require_marker: flag("LIBRARY_STORAGE_REQUIRE_MARKER", true),
            allowed_origins: var("LIBRARY_ALLOWED_ORIGINS", "")
                .split(',')
                .map(|s| s.trim().trim_end_matches('/').to_owned())
                .filter(|s| !s.is_empty())
                .collect(),
            bind: var("LIBRARY_RUST_BIND", "127.0.0.1:8801")
                .parse()
                .map_err(|_| ApiError::bad("LIBRARY_RUST_BIND 无效"))?,
            pool_size: var("LIBRARY_RUST_DB_CONNECTIONS", "12")
                .parse()
                .map_err(|_| ApiError::bad("连接池配置无效"))?,
            query_timeout: Duration::from_secs(10),
            read_only: flag("LIBRARY_RUST_READ_ONLY", true),
        };
        config.validate()?;
        Ok(config)
    }
    pub fn validate(&self) -> Result<()> {
        if !(1..=64).contains(&self.pool_size) {
            return Err(ApiError::bad("数据库连接数必须在 1–64 之间"));
        }
        if self.production
            && (!(8..=20).contains(&self.password.chars().count())
                || self.password == "local-library"
                || self.secret.len() < 32
                || self.secret.starts_with("development-only")
                || !self.database_url.starts_with("mysql"))
        {
            return Err(ApiError::bad(
                "生产环境需要 MySQL、8–20 位独立密码和至少 32 位独立密钥",
            ));
        }
        Ok(())
    }
    pub fn writable(&self) -> Result<()> {
        if self.read_only {
            Err(ApiError(
                axum::http::StatusCode::CONFLICT,
                "Rust 验证实例为只读模式",
            ))
        } else {
            Ok(())
        }
    }
    pub fn sqlx_url(&self) -> String {
        let normalized = self
            .database_url
            .replacen("mysql+pymysql://", "mysql://", 1);
        if normalized.starts_with("mysql://") {
            // PyMySQL's charset option is not a SQLx connection option.
            if let Ok(mut url) = url::Url::parse(&normalized) {
                let query: Vec<_> = url
                    .query_pairs()
                    .filter(|(k, _)| k != "charset")
                    .map(|(k, v)| (k.into_owned(), v.into_owned()))
                    .collect();
                url.set_query(None);
                if !query.is_empty() {
                    url.query_pairs_mut().extend_pairs(query);
                }
                return url.to_string();
            }
        }
        normalized
    }
}
