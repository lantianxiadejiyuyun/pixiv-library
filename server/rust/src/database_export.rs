//! A dedicated read-only MySQL snapshot, streamed to a private temporary file.
//! Generated columns are excluded from INSERTs; encrypted values remain encrypted.
use crate::{
    AppState,
    config::Config,
    error::{ApiError, Result},
};
use axum::{extract::State, response::Response};
use futures_util::TryStreamExt;
use sqlx::{Column, Connection, Executor, Row, TypeInfo, ValueRef};
use std::{str::FromStr, time::Duration};
use tokio::io::AsyncWriteExt;
fn unavailable() -> ApiError {
    ApiError(
        axum::http::StatusCode::SERVICE_UNAVAILABLE,
        "数据库导出失败，请检查连接、表结构和临时磁盘空间",
    )
}
fn identifier(v: &str) -> Result<String> {
    if v.is_empty() || v.contains('\0') {
        return Err(unavailable());
    }
    Ok(format!("`{}`", v.replace('`', "``")))
}
fn text(v: &str) -> String {
    format!(
        "CONVERT(X'{}' USING utf8mb4)",
        v.as_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
    )
}
fn literal(row: &sqlx::mysql::MySqlRow, n: usize) -> Result<String> {
    let raw = row.try_get_raw(n)?;
    if raw.is_null() {
        return Ok("NULL".into());
    }
    let ty = row.columns()[n].type_info().name();
    Ok(match ty {
        "TINYINT" | "SMALLINT" | "MEDIUMINT" | "INT" | "BIGINT" => {
            row.try_get::<i64, _>(n)?.to_string()
        }
        "TINYINT UNSIGNED" | "SMALLINT UNSIGNED" | "MEDIUMINT UNSIGNED" | "INT UNSIGNED"
        | "BIGINT UNSIGNED" => row.try_get::<u64, _>(n)?.to_string(),
        "BOOLEAN" | "TINYINT(1)" => if row.try_get::<bool, _>(n)? { "1" } else { "0" }.into(),
        "FLOAT" | "DOUBLE" => {
            let v = row.try_get::<f64, _>(n)?;
            if !v.is_finite() {
                return Err(unavailable());
            }
            v.to_string()
        }
        "DECIMAL" => row.try_get::<sqlx::types::Decimal, _>(n)?.to_string(),
        "DATETIME" | "TIMESTAMP" => format!(
            "'{}'",
            row.try_get::<chrono::NaiveDateTime, _>(n)?
                .format("%Y-%m-%d %H:%M:%S%.6f")
        ),
        "DATE" => format!("'{}'", row.try_get::<chrono::NaiveDate, _>(n)?),
        "JSON" => text(
            &row.try_get::<sqlx::types::Json<serde_json::Value>, _>(n)?
                .0
                .to_string(),
        ),
        "BINARY" | "VARBINARY" | "BLOB" | "TINYBLOB" | "MEDIUMBLOB" | "LONGBLOB" => format!(
            "X'{}'",
            row.try_get::<Vec<u8>, _>(n)?
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>()
        ),
        _ => text(&row.try_get::<String, _>(n)?),
    })
}
const HEADER: &str = "-- Qiye library database backup. Restore into an EMPTY MySQL database.\n-- Includes encrypted account/settings values, excludes media/models/environment/encryption keys.\nSET @QIYE_OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS;\nSET @QIYE_OLD_SQL_MODE=@@SQL_MODE;\nSET @QIYE_OLD_TIME_ZONE=@@TIME_ZONE;\nSET NAMES utf8mb4;\nSET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\nSET TIME_ZONE='+00:00';\nSET FOREIGN_KEY_CHECKS=0;\n";
struct Writer {
    file: tokio::fs::File,
    size: u64,
}
impl Writer {
    async fn write(&mut self, data: &str) -> Result<()> {
        self.size += data.len() as u64;
        if self.size > 1024 * 1024 * 1024 {
            return Err(unavailable());
        }
        self.file.write_all(data.as_bytes()).await?;
        Ok(())
    }
}
pub async fn dump(config: &Config) -> Result<tempfile::NamedTempFile> {
    if !config.sqlx_url().starts_with("mysql://") {
        return Err(ApiError(
            axum::http::StatusCode::NOT_IMPLEMENTED,
            "数据库导出仅支持 MySQL",
        ));
    }
    let file = tempfile::NamedTempFile::new()?;
    let mut out = Writer {
        file: tokio::fs::File::from_std(file.reopen()?),
        size: 0,
    };
    let options = sqlx::mysql::MySqlConnectOptions::from_str(&config.sqlx_url())?;
    let mut conn = sqlx::MySqlConnection::connect_with(&options).await?;
    for sql in [
        "SET SESSION time_zone='+00:00'",
        "SET SESSION sql_mode='NO_AUTO_VALUE_ON_ZERO'",
        "SET SESSION sql_quote_show_create=1",
        "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
    ] {
        conn.execute(sql).await?;
    }
    let schema: Vec<String> = serde_json::from_str(include_str!("../contracts/mysql-schema.json"))
        .map_err(|_| unavailable())?;
    let re = regex::Regex::new(r"(?i)^\s*CREATE TABLE\s+(\w+)\s*\(").unwrap();
    let mut names = schema
        .iter()
        .filter_map(|s| re.captures(s).map(|c| c[1].to_owned()))
        .collect::<Vec<_>>();
    names.push("alembic_version".into());
    names.sort();
    let mut definitions = vec![];
    for name in &names {
        let engine:Option<String>=sqlx::query_scalar("SELECT CAST(ENGINE AS CHAR CHARACTER SET utf8mb4) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=?").bind(name).fetch_optional(&mut conn).await?;
        if engine.as_deref() != Some("InnoDB") {
            return Err(unavailable());
        }
        let q = identifier(name)?;
        let _ = sqlx::query(&format!("SELECT * FROM {q} LIMIT 0"))
            .fetch_all(&mut conn)
            .await?;
        let columns=sqlx::query("SELECT CAST(COLUMN_NAME AS CHAR CHARACTER SET utf8mb4) AS COLUMN_NAME,CAST(EXTRA AS CHAR CHARACTER SET utf8mb4) AS EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? ORDER BY ORDINAL_POSITION").bind(name).fetch_all(&mut conn).await?.into_iter().filter_map(|r|{let extra:String=r.get("EXTRA");if extra.to_uppercase().contains("GENERATED"){None}else{Some(r.get::<String,_>("COLUMN_NAME"))}}).map(|n|identifier(&n)).collect::<Result<Vec<_>>>()?;
        let show = format!("SHOW CREATE TABLE {q}");
        let ddl = conn
            .fetch_one(show.as_str())
            .await?
            .try_get_unchecked::<String, _>(1)?;
        if !ddl.trim_start().starts_with("CREATE TABLE ") {
            return Err(unavailable());
        }
        definitions.push((q, columns, ddl));
    }
    out.write(HEADER).await?;
    for (_, _, ddl) in &definitions {
        out.write(&format!("{ddl};\n\n")).await?;
    }
    out.write("START TRANSACTION;\n").await?;
    for (table, columns, _) in &definitions {
        let prefix = format!("INSERT INTO {table} ({}) VALUES\n", columns.join(","));
        let query = format!("SELECT {} FROM {table}", columns.join(","));
        let mut rows = sqlx::query(&query).fetch(&mut conn);
        let mut batch = String::new();
        let mut count = 0;
        while let Some(row) = rows.try_next().await? {
            let values = (0..columns.len())
                .map(|n| literal(&row, n))
                .collect::<Result<Vec<_>>>()?;
            let value = format!("({})", values.join(","));
            if count > 0 && (count >= 200 || batch.len() + value.len() > 1024 * 1024) {
                out.write(&format!("{prefix}{batch};\n")).await?;
                batch.clear();
                count = 0;
            }
            if count > 0 {
                batch.push_str(",\n");
            }
            batch.push_str(&value);
            count += 1;
        }
        if count > 0 {
            out.write(&format!("{prefix}{batch};\n")).await?;
        }
    }
    out.write("COMMIT;\nSET FOREIGN_KEY_CHECKS=@QIYE_OLD_FOREIGN_KEY_CHECKS;\nSET SQL_MODE=@QIYE_OLD_SQL_MODE;\nSET TIME_ZONE=@QIYE_OLD_TIME_ZONE;\n-- Backup complete.\n").await?;
    out.file.flush().await?;
    out.file.sync_all().await?;
    conn.close().await?;
    Ok(file)
}
pub async fn export(State(s): State<AppState>) -> Result<Response> {
    if !s.db.mysql() {
        return Err(ApiError(
            axum::http::StatusCode::NOT_IMPLEMENTED,
            "数据库导出仅支持 MySQL；SQLite 开发模式暂不支持",
        ));
    }
    let permit = s.backup_limit.clone().try_acquire_owned().map_err(|_| {
        ApiError(
            axum::http::StatusCode::CONFLICT,
            "已有数据库导出正在进行，请稍后重试",
        )
    })?;
    let file = tokio::time::timeout(Duration::from_secs(600), dump(&s.config))
        .await
        .map_err(|_| unavailable())??;
    let response = crate::item_api::temporary_owned(
        file,
        &format!(
            "qiye-database-{}.sql",
            chrono::Utc::now().format("%Y%m%d-%H%M%S")
        ),
        "application/sql",
        Some(permit),
    )
    .await?;
    Ok(response)
}
