use crate::{config::Config, error::Result};
use chrono::Timelike;
use serde_json::{Map, Value, json};
use sqlx::{
    Column, Row, TypeInfo, ValueRef,
    mysql::{MySqlPoolOptions, MySqlRow},
    sqlite::{SqlitePoolOptions, SqliteRow},
};
use std::str::FromStr;

#[derive(Clone)]
pub enum Db {
    MySql(sqlx::MySqlPool),
    Sqlite(sqlx::SqlitePool),
}
pub enum Tx {
    MySql(sqlx::Transaction<'static, sqlx::MySql>),
    Sqlite(sqlx::Transaction<'static, sqlx::Sqlite>),
}

macro_rules! bind_values {
    ($query:expr, $args:expr) => {{
        let mut query = $query;
        for arg in $args {
            query = match arg {
                Value::Null => query.bind(Option::<String>::None),
                Value::Bool(v) => query.bind(*v),
                Value::Number(v) if v.is_i64() => query.bind(v.as_i64().unwrap()),
                Value::Number(v) => query.bind(v.as_f64().unwrap()),
                Value::String(v) => query.bind(v.clone()),
                v => query.bind(v.to_string()),
            };
        }
        query
    }};
}
fn mysql_row(row: MySqlRow) -> std::result::Result<Value, sqlx::Error> {
    let mut out = Map::new();
    for col in row.columns() {
        let n = col.ordinal();
        let val = if row.try_get_raw(n)?.is_null() {
            Value::Null
        } else {
            match col.type_info().name() {
                "BOOLEAN" | "TINYINT(1)" => json!(row.try_get::<bool, _>(n)?),
                "TINYINT" | "SMALLINT" | "MEDIUMINT" | "INT" | "BIGINT" => {
                    json!(row.try_get::<i64, _>(n)?)
                }
                "TINYINT UNSIGNED" | "SMALLINT UNSIGNED" | "INT UNSIGNED" | "BIGINT UNSIGNED" => {
                    json!(row.try_get::<u64, _>(n)?)
                }
                "FLOAT" | "DOUBLE" => json!(row.try_get::<f64, _>(n)?),
                "DECIMAL" => {
                    let s = row.try_get::<sqlx::types::Decimal, _>(n)?.to_string();
                    if let Ok(v) = s.parse::<i64>() {
                        json!(v)
                    } else {
                        json!(s.parse::<f64>().unwrap_or(0.0))
                    }
                }
                "JSON" => row.try_get::<sqlx::types::Json<Value>, _>(n)?.0,
                // MySQL 8 exposes some metadata/function text as VARBINARY.
                // String's decoder still validates UTF-8; do not coerce actual
                // arbitrary binary payloads or suppress decoding failures.
                "VARBINARY" | "BINARY" => json!(row.try_get_unchecked::<String, _>(n)?),
                "DATETIME" | "TIMESTAMP" => {
                    json!(format_datetime(row.try_get::<chrono::NaiveDateTime, _>(n)?))
                }
                _ => json!(row.try_get::<String, _>(n)?),
            }
        };
        out.insert(col.name().to_owned(), val);
    }
    Ok(Value::Object(out))
}
fn sqlite_row(row: SqliteRow) -> std::result::Result<Value, sqlx::Error> {
    let mut out = Map::new();
    for col in row.columns() {
        let n = col.ordinal();
        let val = if row.try_get_raw(n)?.is_null() {
            Value::Null
        } else {
            match row.try_get_raw(n)?.type_info().name() {
                "BOOLEAN" => json!(row.try_get::<bool, _>(n)?),
                "INTEGER" => json!(row.try_get::<i64, _>(n)?),
                "REAL" => json!(row.try_get::<f64, _>(n)?),
                _ => json!(row.try_get::<String, _>(n)?),
            }
        };
        out.insert(col.name().to_owned(), val);
    }
    Ok(Value::Object(out))
}
macro_rules! methods {
    ($mysql:ident, $sqlite:ident) => {
        pub async fn query(&self, sql: &str, args: &[Value]) -> Result<Vec<Value>> {
            match self {
                Self::MySql(pool) => Ok(bind_values!(sqlx::query(sql), args)
                    .fetch_all(pool)
                    .await?
                    .into_iter()
                    .map(mysql_row)
                    .collect::<std::result::Result<_, _>>()?),
                Self::Sqlite(pool) => Ok(bind_values!(sqlx::query(sql), args)
                    .fetch_all(pool)
                    .await?
                    .into_iter()
                    .map(sqlite_row)
                    .collect::<std::result::Result<_, _>>()?),
            }
        }
        pub async fn execute(&self, sql: &str, args: &[Value]) -> Result<u64> {
            match self {
                Self::MySql(pool) => Ok(bind_values!(sqlx::query(sql), args)
                    .execute(pool)
                    .await?
                    .rows_affected()),
                Self::Sqlite(pool) => Ok(bind_values!(sqlx::query(sql), args)
                    .execute(pool)
                    .await?
                    .rows_affected()),
            }
        }
    };
}
impl Db {
    pub async fn connect(config: &Config) -> Result<Self> {
        let url = config.sqlx_url();
        if url.starts_with("mysql://") {
            Ok(Self::MySql(
                MySqlPoolOptions::new()
                    .min_connections(1)
                    .max_connections(config.pool_size)
                    .acquire_timeout(config.query_timeout)
                    .connect(&url)
                    .await?,
            ))
        } else {
            let options = sqlx::sqlite::SqliteConnectOptions::from_str(&url)?
                .foreign_keys(true)
                .busy_timeout(config.query_timeout);
            Ok(Self::Sqlite(
                SqlitePoolOptions::new()
                    .max_connections(config.pool_size)
                    .acquire_timeout(config.query_timeout)
                    .connect_with(options)
                    .await?,
            ))
        }
    }
    methods!(mysql, sqlite);
    pub fn mysql(&self) -> bool {
        matches!(self, Self::MySql(_))
    }
    pub async fn one(&self, sql: &str, args: &[Value]) -> Result<Option<Value>> {
        Ok(self.query(sql, args).await?.into_iter().next())
    }
    pub async fn count(&self, sql: &str, args: &[Value]) -> Result<i64> {
        Ok(self
            .one(sql, args)
            .await?
            .and_then(|v| v.as_object().and_then(|m| m.values().next()).cloned())
            .and_then(|v| v.as_i64())
            .unwrap_or(0))
    }
    pub async fn begin(&self) -> Result<Tx> {
        match self {
            Self::MySql(p) => Ok(Tx::MySql(p.begin().await?)),
            Self::Sqlite(p) => Ok(Tx::Sqlite(p.begin().await?)),
        }
    }
    pub fn upsert(&self, columns: &str, keys: &str) -> String {
        let columns: Vec<_> = columns.split(',').collect();
        if self.mysql() {
            format!(
                "ON DUPLICATE KEY UPDATE {}",
                columns
                    .iter()
                    .map(|c| format!("{c}=VALUES({c})"))
                    .collect::<Vec<_>>()
                    .join(",")
            )
        } else {
            format!(
                "ON CONFLICT({keys}) DO UPDATE SET {}",
                columns
                    .iter()
                    .map(|c| format!("{c}=excluded.{c}"))
                    .collect::<Vec<_>>()
                    .join(",")
            )
        }
    }
}
impl Tx {
    pub async fn query(&mut self, sql: &str, args: &[Value]) -> Result<Vec<Value>> {
        match self {
            Self::MySql(t) => Ok(bind_values!(sqlx::query(sql), args)
                .fetch_all(&mut **t)
                .await?
                .into_iter()
                .map(mysql_row)
                .collect::<std::result::Result<_, _>>()?),
            Self::Sqlite(t) => Ok(bind_values!(sqlx::query(sql), args)
                .fetch_all(&mut **t)
                .await?
                .into_iter()
                .map(sqlite_row)
                .collect::<std::result::Result<_, _>>()?),
        }
    }
    pub async fn execute(&mut self, sql: &str, args: &[Value]) -> Result<u64> {
        match self {
            Self::MySql(t) => Ok(bind_values!(sqlx::query(sql), args)
                .execute(&mut **t)
                .await?
                .rows_affected()),
            Self::Sqlite(t) => Ok(bind_values!(sqlx::query(sql), args)
                .execute(&mut **t)
                .await?
                .rows_affected()),
        }
    }
    pub async fn commit(self) -> Result<()> {
        match self {
            Self::MySql(t) => t.commit().await?,
            Self::Sqlite(t) => t.commit().await?,
        };
        Ok(())
    }
}
pub fn json_column(value: &Value) -> Value {
    match value {
        Value::String(s) => serde_json::from_str(s).unwrap_or(Value::Null),
        v => v.clone(),
    }
}
pub fn truth(v: &Value) -> bool {
    v.as_bool().unwrap_or_else(|| v.as_i64().unwrap_or(0) != 0)
}
pub fn iso(v: &Value) -> Value {
    if let Some(s) = v.as_str() {
        let s = s.trim_end_matches('Z').replace(' ', "T");
        chrono::NaiveDateTime::parse_from_str(&s, "%Y-%m-%dT%H:%M:%S%.f")
            .ok()
            .map(|dt| json!(format_datetime(dt)))
            .unwrap_or(Value::Null)
    } else {
        Value::Null
    }
}
fn format_datetime(dt: chrono::NaiveDateTime) -> String {
    dt.format(if dt.nanosecond() == 0 {
        "%Y-%m-%dT%H:%M:%SZ"
    } else {
        "%Y-%m-%dT%H:%M:%S%.6fZ"
    })
    .to_string()
}
pub fn now() -> String {
    chrono::Utc::now()
        .naive_utc()
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string()
}
