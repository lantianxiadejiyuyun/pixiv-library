use axum::Json;
use serde_json::Value;
use std::sync::OnceLock;
pub async fn catalog() -> Json<Value> {
    static CATALOG: OnceLock<Value> = OnceLock::new();
    Json(
        CATALOG
            .get_or_init(|| {
                serde_json::from_str(include_str!("../contracts/mobile-catalog.json"))
                    .expect("checked-in administration contract")
            })
            .clone(),
    )
}
