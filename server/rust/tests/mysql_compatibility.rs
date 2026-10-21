//! Explicit opt-in Docker-only database. This suite refuses populated schemas
//! and any database name other than the isolated contract fixture database.
use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use http_body_util::BodyExt;
use qiye_server::{config::Config, db::Db};
use serde_json::{Value, json};
use std::time::Duration;
use tower::ServiceExt;
#[tokio::test]
#[ignore = "requires the disposable qiye-rust-mysql fixture container; see README"]
async fn mysql_84_native_contract_and_transactions() {
    let url = std::env::var("QIYE_RUST_TEST_MYSQL_URL").expect("explicit fixture URL required");
    let parsed = url::Url::parse(&url).unwrap();
    assert_eq!(parsed.host_str(), Some("qiye-rust-mysql"));
    assert_eq!(parsed.path(), "/qiye_rust_contract");
    assert_eq!(parsed.username(), "contract");
    let temp = tempfile::tempdir().unwrap();
    let storage = temp.path().join("storage");
    std::fs::create_dir(&storage).unwrap();
    std::fs::write(storage.join(".library-storage"), "qiye-library-v1\n").unwrap();
    let blobs: Value = serde_json::from_str(include_str!("../contracts/blobs.json")).unwrap();
    for (name, data) in blobs.as_object().unwrap() {
        let path = storage.join(name);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, STANDARD.decode(data.as_str().unwrap()).unwrap()).unwrap();
    }
    let config = Config {
        database_url: url,
        storage_root: storage,
        import_root: temp.path().join("import"),
        frontend: temp.path().join("web"),
        password: "local-library".into(),
        secret: "isolated-rust-contract-fixture-not-production".into(),
        production: false,
        secure_cookie: false,
        require_marker: true,
        allowed_origins: vec![],
        bind: "127.0.0.1:8801".parse().unwrap(),
        pool_size: 4,
        query_timeout: Duration::from_secs(3),
        read_only: false,
    };
    let state = qiye_server::connect(config).await.unwrap();
    assert!(matches!(state.db, Db::MySql(_)));
    assert_eq!(
        state
            .db
            .count(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()",
                &[]
            )
            .await
            .unwrap(),
        0,
        "fixture database must be empty"
    );
    let ddl: Vec<String> =
        serde_json::from_str(include_str!("../contracts/mysql-schema.json")).unwrap();
    for sql in ddl {
        state.db.execute(&sql, &[]).await.unwrap();
    }
    state
        .db
        .execute(
            "CREATE TABLE alembic_version(version_num VARCHAR(32) PRIMARY KEY)",
            &[],
        )
        .await
        .unwrap();
    state
        .db
        .execute(
            "INSERT INTO alembic_version(version_num) VALUES(?)",
            &[json!(qiye_server::migrations::LEGACY_HEAD)],
        )
        .await
        .unwrap();
    let seeds: Vec<Value> =
        serde_json::from_str(include_str!("../contracts/mysql-seed.json")).unwrap();
    for seed in seeds {
        let table = seed["table"].as_str().unwrap();
        let columns = seed["columns"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| format!("`{}`", s.as_str().unwrap()))
            .collect::<Vec<_>>()
            .join(",");
        let values = seed["values"].as_array().unwrap();
        let marks = std::iter::repeat_n("?", values.len())
            .collect::<Vec<_>>()
            .join(",");
        state
            .db
            .execute(
                &format!("INSERT INTO `{table}`({columns}) VALUES({marks})"),
                values,
            )
            .await
            .unwrap();
    }
    let router = qiye_server::router(state.clone());
    let response = router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/auth/login")
                .header("content-type", "application/json")
                .body(Body::from(json!({"password":"local-library"}).to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let cookie = response.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();
    // MySQL FLOAT is 32 bit; the legacy SQLite fixture stores 64-bit REAL.
    // Compare real numbers at 1e-6 while retaining exact integer counts/IDs.
    fn normalize(v: &mut Value) {
        match v {
            Value::Array(a) => {
                for v in a {
                    normalize(v);
                }
            }
            Value::Object(m) => {
                if let Some(Value::Array(tags)) = m.get_mut("tags") {
                    tags.sort_by_key(|t| format!("{}:{}", t["id"], t["origin"]));
                }
                for v in m.values_mut() {
                    normalize(v);
                }
            }
            Value::Number(n) if n.is_f64() => {
                *v = json!((n.as_f64().unwrap() * 1_000_000.0).round() / 1_000_000.0);
            }
            _ => (),
        }
    }
    let snapshots: Vec<Value> =
        serde_json::from_str(include_str!("../contracts/responses.json")).unwrap();
    for snapshot in snapshots {
        let path = snapshot["path"].as_str().unwrap();
        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri(path)
                    .header("cookie", &cookie)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = response.status();
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        let mut actual: Value = serde_json::from_slice(&bytes).unwrap();
        let mut expected = snapshot["body"].clone();
        assert_eq!(status, StatusCode::OK, "{path}: {actual}");
        normalize(&mut actual);
        normalize(&mut expected);
        assert_eq!(actual, expected, "MySQL contract {path}");
    }
    for (path, body) in [
        ("/api/items/item-1/favorite", json!({"favorite":false})),
        ("/api/items/item-1/score", json!({"score":5})),
        (
            "/api/items/item-3/progress",
            json!({"page":0,"section":1,"offset":0.9}),
        ),
        (
            "/api/queue/settings",
            json!({"concurrency":4,"paused":true}),
        ),
    ] {
        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("PUT")
                    .uri(path)
                    .header("cookie", &cookie)
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = response.status();
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(status, 200, "{path}: {}", String::from_utf8_lossy(&bytes));
    }
    assert_eq!(
        state
            .db
            .count("SELECT user_score FROM items WHERE id='item-1'", &[])
            .await
            .unwrap(),
        5
    );
    assert_eq!(
        state
            .db
            .count(
                "SELECT COUNT(*) FROM item_favorites WHERE item_id='item-1'",
                &[]
            )
            .await
            .unwrap(),
        0
    );
    // A full account and more than one claim window of pinned tasks must not
    // hide unassigned work executable by another account.
    state
        .db
        .execute(
            "UPDATE jobs SET status='cancelled',worker_id=NULL,lease_until=NULL",
            &[],
        )
        .await
        .unwrap();
    for id in ["mysql-a", "mysql-b"] {
        let encrypted = qiye_server::secrets::encrypt(
            &state.config.secret,
            &json!({"refresh_token":"synthetic-token-only"}),
        );
        state.db.execute("INSERT INTO source_accounts(id,platform,label,credentials_encrypted,created_at,updated_at) VALUES(?,'pixiv',?,?,?,?)",&[json!(id),json!(id),json!(encrypted),json!(qiye_server::db::now()),json!(qiye_server::db::now())]).await.unwrap();
    }
    qiye_server::secrets::put(&state,qiye_server::accounts::ROLE_KEY,&json!({"enabled":true,"main_account_id":"mysql-a","content_account_ids":["mysql-a","mysql-b"],"detail_account_ids":["mysql-a","mysql-b"],"retry_account_ids":["mysql-a","mysql-b"],"account_limits":{"mysql-a":1,"mysql-b":1}})).await.unwrap();
    state
        .db
        .execute(
            "UPDATE queue_control SET paused=0,concurrency=8,lanes=? WHERE id=1",
            &[json!("{\"download\":{\"concurrency\":8}}")],
        )
        .await
        .unwrap();
    let mut tx = state.db.begin().await.unwrap();
    let busy=qiye_server::tasks::enqueue_tx(&state,&mut tx,"import",&json!({"platform":"pixiv","remote_id":"10000","download":true,"download_account_id":"mysql-a","details_account_id":"mysql-a"})).await.unwrap();
    tx.execute(
        "UPDATE jobs SET status='running',worker_id='fixture-owner',lease_until=? WHERE id=?",
        &[
            json!(
                (chrono::Utc::now() + chrono::Duration::minutes(5))
                    .naive_utc()
                    .format("%Y-%m-%d %H:%M:%S")
                    .to_string()
            ),
            busy["id"].clone(),
        ],
    )
    .await
    .unwrap();
    for n in 0..70 {
        qiye_server::tasks::enqueue_tx(&state,&mut tx,"import",&json!({"platform":"pixiv","remote_id":(20000+n).to_string(),"download":true,"download_account_id":"mysql-a","details_account_id":"mysql-a"})).await.unwrap();
    }
    let eligible = qiye_server::tasks::enqueue_tx(
        &state,
        &mut tx,
        "import",
        &json!({"platform":"pixiv","remote_id":"30000","download":true}),
    )
    .await
    .unwrap();
    tx.commit().await.unwrap();
    let claim = qiye_server::tasks::claim(&state, "download")
        .await
        .unwrap()
        .expect("free account must receive unassigned work behind the pinned backlog");
    assert_eq!(claim.job["id"], eligible["id"]);
    assert_eq!(claim.job["payload"]["download_account_id"], "mysql-b");
    assert_eq!(claim.job["payload"]["details_account_id"], "mysql-b");
    assert!(
        qiye_server::tasks::claim(&state, "download")
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        qiye_server::secrets::get(&state, qiye_server::accounts::ROLE_KEY)
            .await
            .unwrap()
            .unwrap()["enabled"],
        true
    );
    let jobs_before = state
        .db
        .count("SELECT COUNT(*) FROM jobs", &[])
        .await
        .unwrap();
    qiye_server::migrations::verify(&state.config, &state.db)
        .await
        .unwrap();
    let before = state
        .db
        .query("SELECT id,title,user_score FROM items ORDER BY id", &[])
        .await
        .unwrap();
    let backup = qiye_server::database_export::dump(&state.config)
        .await
        .unwrap();
    let sql = std::fs::read_to_string(backup.path()).unwrap();
    assert!(sql.ends_with("-- Backup complete.\n"));
    assert!(!sql.contains(&state.config.secret));
    // Only the disposable, initially empty qiye_rust_contract database above.
    // Round-trip generated columns, JSON, Unicode, decimals and timestamps.
    if let Db::MySql(pool) = &state.db {
        let mut conn = pool.acquire().await.unwrap();
        sqlx::query("SET FOREIGN_KEY_CHECKS=0")
            .execute(&mut *conn)
            .await
            .unwrap();
        let tables: Vec<String> = sqlx::query_scalar(
            "SELECT CAST(TABLE_NAME AS CHAR CHARACTER SET utf8mb4) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()",
        )
        .fetch_all(&mut *conn)
        .await
        .unwrap();
        assert!(tables.len() < 100);
        for table in tables {
            assert!(
                table
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_')
            );
            sqlx::query(&format!("DROP TABLE `{table}`"))
                .execute(&mut *conn)
                .await
                .unwrap();
        }
        sqlx::raw_sql(&sql).execute(&mut *conn).await.unwrap();
        sqlx::query("SET FOREIGN_KEY_CHECKS=1")
            .execute(&mut *conn)
            .await
            .unwrap();
    }
    assert_eq!(
        state
            .db
            .query("SELECT id,title,user_score FROM items ORDER BY id", &[])
            .await
            .unwrap(),
        before
    );
    assert_eq!(
        state
            .db
            .count(
                "SELECT COUNT(*) FROM jobs WHERE queue_lane IS NOT NULL",
                &[]
            )
            .await
            .unwrap(),
        jobs_before
    );
}
