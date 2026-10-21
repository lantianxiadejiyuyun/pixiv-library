use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde_json::json;

#[derive(Debug, thiserror::Error)]
#[error("{1}")]
pub struct ApiError(pub StatusCode, pub &'static str);
pub type Result<T> = std::result::Result<T, ApiError>;
impl ApiError {
    pub fn bad(message: &'static str) -> Self {
        Self(StatusCode::BAD_REQUEST, message)
    }
    pub fn invalid(message: &'static str) -> Self {
        Self(StatusCode::UNPROCESSABLE_ENTITY, message)
    }
    pub fn missing(message: &'static str) -> Self {
        Self(StatusCode::NOT_FOUND, message)
    }
}
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"detail": self.1}))).into_response()
    }
}
impl From<sqlx::Error> for ApiError {
    fn from(error: sqlx::Error) -> Self {
        if let sqlx::Error::Database(ref db) = error
            && db.is_unique_violation()
        {
            return Self(StatusCode::CONFLICT, "记录已存在，请刷新后重试");
        }
        // Never log a connection URL, SQL statement, bind parameter or DB error.
        tracing::warn!(
            kind = match error {
                sqlx::Error::PoolTimedOut => "pool_timeout",
                _ => "database",
            },
            "database request failed"
        );
        Self(
            StatusCode::SERVICE_UNAVAILABLE,
            "数据库暂时不可用，请稍后重试",
        )
    }
}
impl From<std::io::Error> for ApiError {
    fn from(_: std::io::Error) -> Self {
        Self(
            StatusCode::SERVICE_UNAVAILABLE,
            "文件存储不可用，请检查磁盘空间、权限与 NAS 挂载",
        )
    }
}
