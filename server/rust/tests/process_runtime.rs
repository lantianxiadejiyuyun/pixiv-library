//! Launch the actual native binary and all eight worker processes against an
//! isolated SQLite fixture. No production/network platform credentials exist.
#![cfg(target_os = "linux")]
use axum::{Json, Router, routing::post};
use base64::{Engine, engine::general_purpose::STANDARD};
use qiye_server::{config::Config, db::Db};
use serde_json::{Value, json};
use std::{
    process::Stdio,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
async fn poll(mut condition: impl AsyncFnMut() -> bool) {
    for _ in 0..100 {
        if condition().await {
            return;
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    panic!("native process condition timed out");
}
fn gone(pid: u32) -> bool {
    std::fs::read_to_string(format!("/proc/{pid}/stat"))
        .ok()
        .is_none_or(|v| v.split_whitespace().nth(2) == Some("Z"))
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn native_worker_crash_recovers_owner_and_affinity_without_duplicate_writes() {
    let temp = tempfile::tempdir().unwrap();
    let db = temp.path().join("fixture.db");
    std::fs::write(&db, "").unwrap();
    let storage = temp.path().join("storage");
    std::fs::create_dir(&storage).unwrap();
    std::fs::write(storage.join(".library-storage"), "qiye-library-v1\n").unwrap();
    let blobs: Value = serde_json::from_str(include_str!("../contracts/blobs.json")).unwrap();
    for (name, value) in blobs.as_object().unwrap() {
        let path = storage.join(name);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, STANDARD.decode(value.as_str().unwrap()).unwrap()).unwrap();
    }
    let config = Config {
        database_url: format!("sqlite://{}", db.display()),
        storage_root: storage,
        import_root: temp.path().join("import"),
        frontend: temp.path().join("web"),
        password: "runtime-test-only".into(),
        secret: "isolated-native-runtime-fixture-secret".into(),
        production: false,
        secure_cookie: false,
        require_marker: true,
        allowed_origins: vec![],
        bind: "127.0.0.1:0".parse().unwrap(),
        pool_size: 4,
        query_timeout: Duration::from_secs(3),
        read_only: false,
    };
    let s = qiye_server::connect(config.clone()).await.unwrap();
    if let Db::Sqlite(pool) = &s.db {
        let mut conn = pool.acquire().await.unwrap();
        sqlx::query("PRAGMA foreign_keys=OFF")
            .execute(&mut *conn)
            .await
            .unwrap();
        sqlx::raw_sql(include_str!("../contracts/fixture.txt"))
            .execute(&mut *conn)
            .await
            .unwrap();
        sqlx::query("PRAGMA foreign_keys=ON")
            .execute(&mut *conn)
            .await
            .unwrap();
    }
    s.db.execute(
        "CREATE TABLE alembic_version(version_num VARCHAR(32) PRIMARY KEY)",
        &[],
    )
    .await
    .unwrap();
    s.db.execute(
        "INSERT INTO alembic_version VALUES(?)",
        &[json!(qiye_server::migrations::LEGACY_HEAD)],
    )
    .await
    .unwrap();
    s.db.execute("UPDATE jobs SET status='cancelled'", &[])
        .await
        .unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let vision = listener.local_addr().unwrap();
    let requests = Arc::new(AtomicUsize::new(0));
    let release = Arc::new(tokio::sync::Notify::new());
    let calls = requests.clone();
    let notify = release.clone();
    let service = tokio::spawn(async move {
        axum::serve(listener,Router::new().route("/v1/vision/analyze",post(move||{let calls=calls.clone();let notify=notify.clone();async move{if calls.fetch_add(1,Ordering::SeqCst)==0{notify.notified().await;}Json(json!({"rating":"general","rating_scores":{"general":0.99},"model_version":"runtime-fixture","tags":[{"name":"测试标签","category":0,"score":0.9}]}))}}))).await.unwrap()
    });
    qiye_server::secrets::put(&s,qiye_server::ai_nodes::KEY,&json!({"image_node_id":"test-node","nodes":[{"id":"test-node","enabled":true,"label":"fixture","base_url":format!("http://{vision}"),"key":"runtime-fixture-key-0123456789012345"}]})).await.unwrap();
    let address = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let bind = address.local_addr().unwrap();
    drop(address);
    let mut process = tokio::process::Command::new(env!("CARGO_BIN_EXE_qiye-server"))
        .env_clear()
        .env("LIBRARY_DATABASE_URL", &config.database_url)
        .env("LIBRARY_STORAGE_ROOT", &config.storage_root)
        .env("LIBRARY_IMPORT_ROOT", &config.import_root)
        .env("LIBRARY_FRONTEND_DIST", &config.frontend)
        .env("LIBRARY_ADMIN_PASSWORD", &config.password)
        .env("LIBRARY_SECRET_KEY", &config.secret)
        .env("LIBRARY_RUST_READ_ONLY", "false")
        .env("LIBRARY_RUST_WORKERS_ENABLED", "true")
        .env("LIBRARY_RUST_BIND", bind.to_string())
        .env("LIBRARY_CPU_AFFINITY", "auto")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let client = reqwest::Client::new();
    let root = format!("http://{bind}");
    poll(async || {
        client
            .get(format!("{root}/api/health"))
            .send()
            .await
            .is_ok()
    })
    .await;
    let login = client
        .post(format!("{root}/api/auth/login"))
        .json(&json!({"password":config.password}))
        .send()
        .await
        .unwrap();
    assert!(login.status().is_success());
    let cookie = login.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();
    let mut workers = vec![];
    poll(async || {
        let Ok(r) = client
            .get(format!("{root}/api/queue/status"))
            .header("cookie", &cookie)
            .send()
            .await
        else {
            return false;
        };
        let Ok(v) = r.json::<Value>().await else {
            return false;
        };
        workers = v["runtime"]["workers"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        workers.len() == 8 && workers.iter().all(|w| w["running"] == true)
    })
    .await;
    for worker in &workers {
        let pid = worker["pid"].as_u64().unwrap();
        let status = std::fs::read_to_string(format!("/proc/{pid}/status")).unwrap();
        let allowed = status
            .lines()
            .find(|l| l.starts_with("Cpus_allowed_list:"))
            .unwrap()
            .split_whitespace()
            .nth(1)
            .unwrap();
        let expected = worker["cpus"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap())
            .collect::<Vec<_>>();
        for cpu in expected {
            assert!(
                allowed.split(',').any(|range| {
                    let (a, b) = range.split_once('-').unwrap_or((range, range));
                    (a.parse::<u64>().unwrap()..=b.parse::<u64>().unwrap()).contains(&cpu)
                }),
                "affinity was not applied"
            );
        }
    }
    let mut tx = s.db.begin().await.unwrap();
    let job = qiye_server::tasks::enqueue_tx(&s, &mut tx, "ai", &json!({"item_id":"item-1"}))
        .await
        .unwrap();
    tx.commit().await.unwrap();
    let id = job["id"].as_str().unwrap();
    poll(async || requests.load(Ordering::SeqCst) > 0).await;
    let ai = workers.iter().find(|w| w["lane"] == "ai").unwrap();
    let old_pid = ai["pid"].as_u64().unwrap() as u32;
    let old =
        s.db.one("SELECT worker_id FROM jobs WHERE id=?", &[json!(id)])
            .await
            .unwrap()
            .unwrap()["worker_id"]
            .clone();
    assert!(!old.is_null());
    unsafe {
        assert_eq!(libc::kill(old_pid as i32, libc::SIGKILL), 0);
    }
    poll(async || {
        s.db.one("SELECT status FROM jobs WHERE id=?", &[json!(id)])
            .await
            .unwrap()
            .is_some_and(|v| v["status"] == "completed")
    })
    .await;
    release.notify_waiters();
    let row =
        s.db.one(
            "SELECT attempts,worker_id FROM jobs WHERE id=?",
            &[json!(id)],
        )
        .await
        .unwrap()
        .unwrap();
    assert_eq!(row["attempts"], 2);
    assert!(row["worker_id"].is_null());
    assert!(requests.load(Ordering::SeqCst) >= 3);
    assert_eq!(
        s.db.count(
            "SELECT COUNT(*) FROM item_tags WHERE item_id='item-1' AND origin='ai'",
            &[]
        )
        .await
        .unwrap(),
        1
    );
    process.kill().await.unwrap();
    let _ = process.wait().await;
    poll(async || {
        workers
            .iter()
            .all(|w| gone(w["pid"].as_u64().unwrap() as u32))
    })
    .await;
    service.abort();
}
