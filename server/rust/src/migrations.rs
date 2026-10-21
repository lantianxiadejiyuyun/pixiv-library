//! Explicit baseline initialization. Existing libraries keep all their data;
//! unknown revisions are refused instead of being guessed or reset.
use crate::{
    config::Config,
    db::{Db, now},
    error::{ApiError, Result},
};
use serde_json::json;
pub const LEGACY_HEAD: &str = "e71b4c8213de";
pub async fn initialize(config: &Config, db: &Db) -> Result<()> {
    let tables=db.query(if db.mysql(){"SELECT TABLE_NAME AS name FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()"}else{"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"},&[]).await?;
    if !tables.is_empty() {
        let has_revision = tables.iter().any(|v| v["name"] == "alembic_version");
        if !has_revision {
            return Err(ApiError::bad(
                "数据库非空且缺少已知版本标记，未修改任何数据",
            ));
        }
        let revisions = db
            .query("SELECT version_num FROM alembic_version", &[])
            .await?;
        if revisions.len() != 1 || revisions[0]["version_num"] != LEGACY_HEAD {
            return Err(ApiError::bad(
                "数据库版本不兼容，请先由原服务升级或在空数据库初始化",
            ));
        }
        let names: std::collections::HashSet<_> =
            tables.iter().filter_map(|v| v["name"].as_str()).collect();
        for table in [
            "items",
            "assets",
            "jobs",
            "tags",
            "source_accounts",
            "queue_control",
            "network_settings",
            "author_profiles",
            "author_feeds",
            "subscriptions",
            "device_sessions",
            "pixiv_watchlist",
        ] {
            if !names.contains(table) {
                return Err(ApiError::bad("数据库表不完整，未自动修改现有结构"));
            }
        }
        tracing::info!("Existing library schema verified; no data changed");
        return Ok(());
    }
    config.writable()?;
    let statements: Vec<String> = serde_json::from_str(if db.mysql() {
        include_str!("../contracts/mysql-schema.json")
    } else {
        include_str!("../contracts/sqlite-schema.json")
    })
    .map_err(|_| ApiError::bad("内置数据库结构无效"))?;
    // MySQL auto-commits DDL. An interrupted baseline remains unversioned and
    // requires inspection, rather than silently adopting a partial schema.
    for statement in statements {
        db.execute(&statement, &[]).await?;
    }
    db.execute(
        "CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL PRIMARY KEY)",
        &[],
    )
    .await?;
    let mut tx = db.begin().await?;
    for (i, name) in ["想看", "在看", "已看", "推荐"].iter().enumerate() {
        tx.execute(
            "INSERT INTO favorite_groups(id,name,created_at,updated_at) VALUES(?,?,?,?)",
            &[
                json!(format!("57135c87-cd5f-4cbd-9e46-{:012}", i + 1)),
                json!(name),
                json!(now()),
                json!(now()),
            ],
        )
        .await?;
    }
    tx.execute(
        "INSERT INTO alembic_version(version_num) VALUES(?)",
        &[json!(LEGACY_HEAD)],
    )
    .await?;
    tx.commit().await?;
    tracing::info!("Empty library schema initialized");
    Ok(())
}
pub async fn verify(config: &Config, db: &Db) -> Result<()> {
    let mut checking = config.clone();
    checking.read_only = true;
    initialize(&checking, db).await
}
