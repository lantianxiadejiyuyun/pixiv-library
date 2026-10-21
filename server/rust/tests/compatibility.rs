use axum::{
    Router,
    body::Body,
    http::{Request, StatusCode},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use http_body_util::BodyExt;
use qiye_server::{AppState, config::Config, db::Db};
use serde_json::{Value, json};
use std::{path::PathBuf, time::Duration};
use tower::ServiceExt;

#[tokio::test]
async fn native_subscriptions_keep_author_bookmarks_independent_and_manual_downloads_separate() {
    let app = TestApp::new(false).await;
    let (status, sources) = app.request("GET", "/api/sources", None).await;
    assert_eq!(status, 200);
    assert_eq!(sources.as_array().unwrap().len(), 5);
    let (status, sub) = app
        .request(
            "POST",
            "/api/subscriptions",
            Some(json!({"platform":"pixiv","target_type":"author","target":"77","label":"作者甲"})),
        )
        .await;
    assert_eq!(status, 200);
    assert_eq!(sub["content_scope"], "all");
    assert_eq!(sub["download_works"], false);
    let path = format!("/api/subscriptions/{}", sub["id"].as_str().unwrap());
    assert_eq!(
        app.request(
            "PUT",
            "/api/pixiv/follow-download-settings",
            Some(json!({"enabled":true}))
        )
        .await
        .0,
        200
    );
    let (_, subs) = app.request("GET", "/api/subscriptions", None).await;
    assert_eq!(subs["items"][0]["download_works"], true);
    assert_eq!(subs["items"][0]["download_bookmarks"], false);
    let (status, sub) = app
        .request(
            "PATCH",
            &path,
            Some(json!({"download_works":false,"download_bookmarks":true})),
        )
        .await;
    assert_eq!(status, 200);
    assert_eq!(sub["download_works"], false);
    assert_eq!(sub["download_bookmarks"], true);
    let input = json!({"tags":["猫","犬"],"platform":"pixiv","auto_download":true});
    let (status, first) = app
        .request("POST", "/api/tag-tracking", Some(input.clone()))
        .await;
    assert_eq!(status, 200);
    let (_, second) = app.request("POST", "/api/tag-tracking", Some(input)).await;
    assert_eq!(first["job_ids"], second["job_ids"]);
    assert_eq!(
        app.request("PUT", "/api/queue/settings", Some(json!({"paused":true})))
            .await
            .0,
        200
    );
    let (status, job) = app
        .request(
            "POST",
            "/api/sources/kemono/import",
            Some(json!({"remote_id":"fanbox:123:456"})),
        )
        .await;
    assert_eq!(status, 200);
    assert_eq!(job["queue"], "manual_download");
    app.request("POST", "/api/jobs/job-2/cancel", None).await;
    let claim = qiye_server::tasks::claim(&app.state, "manual_download")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(claim.job["id"], job["id"]);
    assert!(
        qiye_server::tasks::claim(&app.state, "checks")
            .await
            .unwrap()
            .is_none()
    );
}
#[tokio::test]
async fn author_views_use_local_counts_and_feed_permissions_without_network() {
    let app = TestApp::new(false).await;
    let (status, author) = app.request("GET", "/api/pixiv/authors/77", None).await;
    assert_eq!(status, 200);
    assert_eq!(author["name"], "作者甲");
    assert_eq!(author["local_counts"]["manga"], 1);
    assert_eq!(author["local_counts"]["novel"], 1);
    assert_eq!(author["can_read_private"], false);
    let (status, page) = app
        .request("GET", "/api/pixiv/authors/77/feed?category=manga", None)
        .await;
    assert_eq!(status, 200);
    assert_eq!(page["items"][0]["id"], "item-1");
    assert_eq!(page["local_only"], true);
    let (status, opened) = app
        .request(
            "POST",
            "/api/pixiv/authors/open",
            Some(json!({"url_or_id":"77","prefer_cached":true})),
        )
        .await;
    assert_eq!(status, 200);
    assert_eq!(opened["category"], "manga");
    assert_eq!(opened["cached"], true);
    let (status, watch) = app.request("GET", "/api/pixiv/watchlist", None).await;
    assert_eq!(status, 200);
    assert_eq!(watch["sync"]["configured"], false);
    assert_eq!(watch["total"], 0);
    let (status, _) = app
        .request(
            "POST",
            "/api/pixiv/authors/77/feed",
            Some(json!({"section":"bookmarks","restrict":"private"})),
        )
        .await;
    assert_eq!(status, 409);
    let (status, oauth) = app
        .request("POST", "/api/sources/pixiv/oauth/start", Some(json!({})))
        .await;
    assert_eq!(status, 200);
    assert!(
        oauth["authorization_url"]
            .as_str()
            .unwrap()
            .starts_with("https://app-api.pixiv.net/web/v1/login?")
    );
    assert!(oauth.get("code_verifier").is_none());
    let (status,err)=app.request("POST","/api/sources/pixiv/oauth/finish",Some(json!({"session_id":oauth["session_id"],"code":"https://evil.example/?code=secret-code","label":"测试"}))).await;
    assert_eq!(status, 400);
    assert!(!err.to_string().contains("secret-code"));
}
#[tokio::test]
async fn content_staging_validates_every_file_before_publishing_and_preserves_manual_metadata() {
    use qiye_server::content_store::{self, Staging, Work};
    let app = TestApp::new(false).await;
    let png = std::fs::read(app.state.config.storage_root.join("fixture/page.png")).unwrap();
    let mut staging = Staging::new().unwrap();
    staging.bytes("001.png", &png, None).await.unwrap();
    staging
        .bytes("002.png", b"invalid image", None)
        .await
        .unwrap();
    assert!(
        content_store::prepare(&app.state, "pixiv", staging, None, None)
            .await
            .is_err()
    );
    assert!(
        !app.state
            .config
            .storage_root
            .join("collections/pixiv")
            .exists()
    );
    let mut staging = Staging::new().unwrap();
    staging
        .bytes("novel.txt", b"\xef\xbb\xbftext", None)
        .await
        .unwrap();
    staging
        .bytes("inline.png", &png, Some("uploadedimage:9".into()))
        .await
        .unwrap();
    let prepared = content_store::prepare(&app.state, "pixiv", staging, None, None)
        .await
        .unwrap();
    assert_eq!(prepared.assets.len(), 2);
    assert_eq!(prepared.assets[0].size, 4);
    assert_eq!(prepared.assets[1].width, Some(8));
    let mut work = Work::new("new-novel", "原始标题", "novel").unwrap();
    work.author_id = Some("77".into());
    let mut tx = app.state.db.begin().await.unwrap();
    let item = content_store::merge(&app.state, &mut tx, "pixiv", &work)
        .await
        .unwrap();
    let id = item["id"].as_str().unwrap();
    content_store::attach(&mut tx, id, &prepared.assets, false)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    let (_, view) = app.request("GET", &format!("/api/items/{id}"), None).await;
    assert_eq!(view["page_count"], 1);
    assert_eq!(view["inline_assets"].as_array().unwrap().len(), 1);
    assert_eq!(view["content_state"], "ready");
    app.state.db.execute("UPDATE items SET title='手动修改',rating='general',rating_origin='manual',user_score=5 WHERE id=?",&[json!(id)]).await.unwrap();
    work.title = "远端新标题".into();
    work.published = Some("2020-01-02 03:04:05".into());
    let mut tx = app.state.db.begin().await.unwrap();
    let current = content_store::merge(&app.state, &mut tx, "pixiv", &work)
        .await
        .unwrap();
    assert_eq!(current["title"], "手动修改");
    assert_eq!(current["user_score"], 5);
    assert_eq!(current["rating_origin"], "manual");
    tx.commit().await.unwrap();
    std::fs::remove_file(app.state.config.storage_root.join(".library-storage")).unwrap();
    assert!(
        content_store::prepare(&app.state, "pixiv", Staging::new().unwrap(), None, None)
            .await
            .is_err()
    );
}

#[tokio::test]
async fn character_confirmation_preserves_tags_and_wall_filters_are_local() {
    let app = TestApp::new(false).await;
    let (status, confirmed) = app
        .request(
            "POST",
            "/api/ai/characters/confirm",
            Some(json!({"item_id":"item-1","name":"初音ミク"})),
        )
        .await;
    assert_eq!(status, 200, "{confirmed}");
    let tag = confirmed["tag"]["id"].as_str().unwrap();
    let (_, wall) = app
        .request("GET", "/api/characters?downloaded=true", None)
        .await;
    let row = wall["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["id"] == tag)
        .unwrap();
    assert_eq!(row["count"], 1);
    assert_eq!(row["covers"][0]["id"], "item-1");
    assert_eq!(
        app.request("GET", &format!("/api/characters/{tag}?source=jm"), None)
            .await
            .1["count"],
        0
    );
    let before = app
        .state
        .db
        .count("SELECT COUNT(*) FROM item_tags WHERE item_id='item-1'", &[])
        .await
        .unwrap();
    assert_eq!(
        app.request(
            "POST",
            "/api/ai/characters/confirm",
            Some(json!({"item_id":"item-1","name":"角色:初音ミク"}))
        )
        .await
        .0,
        200
    );
    assert_eq!(
        app.state
            .db
            .count("SELECT COUNT(*) FROM item_tags WHERE item_id='item-1'", &[])
            .await
            .unwrap(),
        before
    );
    assert_eq!(
        app.request("GET", "/api/characters?q=%25", None).await.1["total"],
        0
    );
    assert_eq!(
        app.request("POST", "/api/characters", Some(json!({"candidate_id":0})))
            .await
            .0,
        422
    );
}
#[tokio::test]
async fn encrypted_proxy_cache_observes_external_changes_without_exposing_secrets() {
    let app = TestApp::new(false).await;
    let v = json!({"enabled":true,"selected_node_id":"q-fixture","subscriptions":[{"id":"sub","label":"fixture","url":"https://example.com/feed?token=secret-fixture","auto_update":false,"interval_hours":24,"fetch_via_proxy":false,"nodes":[{"id":"q-fixture","name":"fixture node","config":{"type":"ss","name":"fixture node","server":"example.com","port":443,"cipher":"aes-128-gcm","password":"secret-node-fixture"}}]}]});
    qiye_server::secrets::put(&app.state, "managed_proxy", &v)
        .await
        .unwrap();
    assert!(
        qiye_server::network::outbound_proxy(&app.state)
            .await
            .unwrap()
            .unwrap()
            .starts_with("http://127.0.0.1:")
    );
    let (_, public) = app.request("GET", "/api/network/managed", None).await;
    assert_eq!(public["nodes"][0]["name"], "fixture node");
    assert!(!public.to_string().contains("secret-"));
    assert!(!public.to_string().contains("example.com/feed"));
    let raw = qiye_server::secrets::encrypt(
        &app.state.config.secret,
        &json!({"enabled":false,"subscriptions":[],"selected_node_id":null}),
    );
    app.state
        .db
        .execute(
            "UPDATE network_settings SET value_encrypted=? WHERE `key`='managed_proxy'",
            &[json!(raw)],
        )
        .await
        .unwrap();
    assert!(
        qiye_server::network::outbound_proxy(&app.state)
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        app.request(
            "POST",
            "/api/network/managed/enable",
            Some(json!({"enabled":true}))
        )
        .await
        .0,
        400
    );
    assert_eq!(
        app.request(
            "POST",
            "/api/network/managed/subscriptions",
            Some(json!({"label":"bad","url":"https://127.0.0.1/feed"}))
        )
        .await
        .0,
        400
    );
}
#[tokio::test]
async fn independent_vision_and_ollama_execute_native_jobs_and_preserve_original_data() {
    use axum::{Json, routing::post};
    use std::sync::Arc;
    let app = TestApp::new(false).await;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let hits = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let image_hits = hits.clone();
    let fake=Router::new().route("/v1/vision/analyze",post(move|headers:axum::http::HeaderMap,bytes:axum::body::Bytes|{let hits=image_hits.clone();async move{assert_eq!(headers["authorization"],"Bearer fixture-only-0123456789012345678901234567");assert!(!bytes.is_empty());hits.fetch_add(1,std::sync::atomic::Ordering::SeqCst);Json(json!({"rating":"general","rating_scores":{"general":0.99},"model_version":"test-independent","execution":{"provider":"cpu"},"tags":[{"name":"角色:测试角色","score":0.98,"category":4}]}))}})).route("/api/chat",post(|Json(body):Json<Value>|async move{let input:Value=serde_json::from_str(body["messages"][1]["content"].as_str().unwrap()).unwrap();let tags=input["tags"].as_array().unwrap().iter().map(|t|json!({"id":t["id"],"text":format!("中文{}",t["text"].as_str().unwrap())})).collect::<Vec<_>>();Json(json!({"done":true,"message":{"content":json!({"title":input["title"].as_str().map(|_|"中文标题"),"tags":tags}).to_string()}}))}));
    let service = tokio::spawn(async move { axum::serve(listener, fake).await.unwrap() });
    app.state
        .db
        .execute("UPDATE jobs SET status='cancelled'", &[])
        .await
        .unwrap();
    app.state
        .db
        .execute(
            "UPDATE items SET rating='adult',rating_origin='source' WHERE id='item-1'",
            &[],
        )
        .await
        .unwrap();
    assert_eq!(
        app.request(
            "POST",
            "/api/ai/characters/confirm",
            Some(json!({"item_id":"item-1","name":"人工角色"}))
        )
        .await
        .0,
        200
    );
    let (status,node)=app.request("POST","/api/ai/nodes",Some(json!({"label":"fixture","base_url":format!("http://{address}"),"key":"fixture-only-0123456789012345678901234567"}))).await;
    assert_eq!(status, 200, "{node}");
    let node_id = node["id"]
        .as_str()
        .or_else(|| node["items"][0]["id"].as_str())
        .unwrap();
    assert_eq!(
        app.request(
            "PUT",
            "/api/ai/nodes/routing",
            Some(json!({"image_node_id":node_id}))
        )
        .await
        .0,
        200
    );
    assert_eq!(
        app.request(
            "POST",
            "/api/ai/analyze",
            Some(json!({"item_ids":["item-1"]}))
        )
        .await
        .0,
        200
    );
    let claim = qiye_server::tasks::claim(&app.state, "ai")
        .await
        .unwrap()
        .unwrap();
    let id = claim.job["id"].clone();
    qiye_server::tasks::execute(
        app.state.clone(),
        claim,
        Arc::new(qiye_server::executor::NativeExecutor),
        tokio_util::sync::CancellationToken::new(),
    )
    .await
    .unwrap();
    let result = app
        .state
        .db
        .one("SELECT * FROM jobs WHERE id=?", &[id])
        .await
        .unwrap()
        .unwrap();
    assert_eq!(result["status"], "completed", "{result}");
    assert_eq!(hits.load(std::sync::atomic::Ordering::SeqCst), 2);
    let (_, item) = app.request("GET", "/api/items/item-1", None).await;
    assert_eq!(item["rating"], "adult");
    assert_eq!(
        app.state
            .db
            .count(
                "SELECT COUNT(*) FROM item_tags it JOIN tags t ON t.id=it.tag_id WHERE item_id='item-1' AND origin='manual' AND t.name='角色:人工角色'",
                &[]
            )
            .await
            .unwrap(),
        1
    );
    assert_eq!(app.request("PUT","/api/translation/settings",Some(json!({"enabled":true,"provider":"ollama","base_url":format!("http://{address}"),"model":"test-model","use_proxy":false}))).await.0,200);
    assert_eq!(
        app.request(
            "POST",
            "/api/items/item-1/translate",
            Some(json!({"consent":true,"force":true}))
        )
        .await
        .0,
        200
    );
    let claim = qiye_server::tasks::claim(&app.state, "translation")
        .await
        .unwrap()
        .unwrap();
    let id = claim.job["id"].clone();
    qiye_server::tasks::execute(
        app.state.clone(),
        claim,
        Arc::new(qiye_server::executor::NativeExecutor),
        tokio_util::sync::CancellationToken::new(),
    )
    .await
    .unwrap();
    let result = app
        .state
        .db
        .one("SELECT * FROM jobs WHERE id=?", &[id])
        .await
        .unwrap()
        .unwrap();
    assert_eq!(result["status"], "completed", "{result}");
    let row = app
        .state
        .db
        .one(
            "SELECT title,title_zh,title_zh_source FROM items WHERE id='item-1'",
            &[],
        )
        .await
        .unwrap()
        .unwrap();
    assert_eq!(row["title_zh"], "中文标题");
    assert_eq!(row["title"], row["title_zh_source"]);
    assert_ne!(row["title"], row["title_zh"]);
    // Configuration changes invalidate persisted translation jobs.
    app.request(
        "POST",
        "/api/items/item-1/translate",
        Some(json!({"consent":true,"force":true})),
    )
    .await;
    let claim = qiye_server::tasks::claim(&app.state, "translation")
        .await
        .unwrap()
        .unwrap();
    let id = claim.job["id"].clone();
    app.request("PUT","/api/translation/settings",Some(json!({"enabled":true,"provider":"ollama","base_url":format!("http://{address}"),"model":"changed-model"}))).await;
    qiye_server::tasks::execute(
        app.state.clone(),
        claim,
        Arc::new(qiye_server::executor::NativeExecutor),
        tokio_util::sync::CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(
        app.state
            .db
            .one("SELECT status FROM jobs WHERE id=?", &[id])
            .await
            .unwrap()
            .unwrap()["status"],
        "failed"
    );
    service.abort();
}
#[tokio::test]
async fn archive_import_rejects_traversal_and_sorts_pages_naturally() {
    use std::io::Write;
    let app = TestApp::new(false).await;
    let make = |names: &[&str]| {
        let mut out = std::io::Cursor::new(vec![]);
        {
            let mut zip = zip::ZipWriter::new(&mut out);
            for name in names {
                zip.start_file(*name, zip::write::FileOptions::default())
                    .unwrap();
                zip.write_all(b"synthetic utf8 text").unwrap();
            }
            zip.finish().unwrap();
        }
        out.into_inner()
    };
    let mut unsafe_archive = qiye_server::content_store::Staging::new().unwrap();
    unsafe_archive
        .bytes("test.zip", &make(&["../escape.txt"]), None)
        .await
        .unwrap();
    assert!(unsafe_archive.expand(&app.state).await.is_err());
    let mut safe = qiye_server::content_store::Staging::new().unwrap();
    safe.bytes(
        "test.zip",
        &make(&["page10.txt", "page2.txt", "page1.txt"]),
        None,
    )
    .await
    .unwrap();
    let expanded = safe.expand(&app.state).await.unwrap();
    assert_eq!(
        expanded
            .entries
            .iter()
            .map(|e| e.name.as_str())
            .collect::<Vec<_>>(),
        vec!["page1.txt", "page2.txt", "page10.txt"]
    );
}
#[tokio::test]
async fn baseline_initialization_refuses_unknown_or_partial_databases() {
    let app = TestApp::new(false).await;
    assert!(
        qiye_server::migrations::initialize(&app.state.config, &app.state.db)
            .await
            .is_err()
    );
    let empty = app._temp.path().join("empty.db");
    std::fs::write(&empty, "").unwrap();
    let mut config = app.state.config.clone();
    config.database_url = format!("sqlite://{}", empty.display());
    let s = qiye_server::connect(config).await.unwrap();
    qiye_server::migrations::initialize(&s.config, &s.db)
        .await
        .unwrap();
    qiye_server::migrations::verify(&s.config, &s.db)
        .await
        .unwrap();
    assert_eq!(
        s.db.count("SELECT COUNT(*) FROM favorite_groups", &[])
            .await
            .unwrap(),
        4
    );
    s.db.execute(
        "UPDATE alembic_version SET version_num='unknown-fixture'",
        &[],
    )
    .await
    .unwrap();
    assert!(
        qiye_server::migrations::initialize(&s.config, &s.db)
            .await
            .is_err()
    );
    assert_eq!(
        s.db.count("SELECT COUNT(*) FROM favorite_groups", &[])
            .await
            .unwrap(),
        4
    );
}
#[tokio::test]
async fn speed_streams_have_no_transform_and_release_the_slot_when_disconnected() {
    let app = TestApp::new(false).await;
    let make = || {
        Request::builder()
            .uri("/api/network/speed/download?bytes=65536")
            .header("cookie", &app.cookie)
            .body(Body::empty())
            .unwrap()
    };
    let held = app.router.clone().oneshot(make()).await.unwrap();
    assert_eq!(held.status(), 200);
    assert!(
        held.headers()["cache-control"]
            .to_str()
            .unwrap()
            .contains("no-transform")
    );
    assert_eq!(
        app.router.clone().oneshot(make()).await.unwrap().status(),
        429
    );
    drop(held);
    let response = app.router.clone().oneshot(make()).await.unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(
        response
            .into_body()
            .collect()
            .await
            .unwrap()
            .to_bytes()
            .len(),
        65536
    );
    let response = app
        .router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/network/speed/upload")
                .header("cookie", &app.cookie)
                .header("content-type", "application/octet-stream")
                .header("content-length", "1024")
                .body(Body::from(vec![7u8; 1024]))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
}
#[tokio::test]
async fn managed_proxy_startup_holds_platform_jobs_without_failing_or_blocking_independent_ai() {
    let app = TestApp::new(false).await;
    qiye_server::secrets::put(
        &app.state,
        "managed_proxy",
        &json!({"enabled":true,"subscriptions":[],"selected_node_id":null}),
    )
    .await
    .unwrap();
    assert!(
        qiye_server::tasks::claim(&app.state, "manual_download")
            .await
            .unwrap()
            .is_none()
    );
    let (_, queues) = app.request("GET", "/api/queue/status", None).await;
    let manual = queues["lanes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|l| l["id"] == "manual_download")
        .unwrap();
    // With no local supervisor it reports worker availability first. A live
    // worker reports the proxy wait reason and never consumes the task.
    assert_eq!(manual["counts"]["queued"], 1);
    assert_eq!(
        app.request(
            "POST",
            "/api/ai/analyze",
            Some(json!({"item_ids":["item-1"]}))
        )
        .await
        .0,
        200
    );
    assert!(
        qiye_server::tasks::claim(&app.state, "ai")
            .await
            .unwrap()
            .is_some()
    );
    qiye_server::secrets::put(
        &app.state,
        "managed_proxy",
        &json!({"enabled":false,"subscriptions":[],"selected_node_id":null}),
    )
    .await
    .unwrap();
    assert!(
        qiye_server::tasks::claim(&app.state, "manual_download")
            .await
            .unwrap()
            .is_some()
    );
}
struct TestApp {
    router: Router,
    state: AppState,
    cookie: String,
    _temp: tempfile::TempDir,
}
impl TestApp {
    async fn new(read_only: bool) -> Self {
        let temp = tempfile::tempdir().unwrap();
        let db = temp.path().join("test.db");
        std::fs::write(&db, "").unwrap();
        let storage = temp.path().join("storage");
        std::fs::create_dir(&storage).unwrap();
        std::fs::write(storage.join(".library-storage"), "qiye-library-v1\n").unwrap();
        let blobs: Value = serde_json::from_str(include_str!("../contracts/blobs.json")).unwrap();
        for (name, value) in blobs.as_object().unwrap() {
            let file = storage.join(name);
            std::fs::create_dir_all(file.parent().unwrap()).unwrap();
            std::fs::write(file, STANDARD.decode(value.as_str().unwrap()).unwrap()).unwrap();
        }
        let config = Config {
            database_url: format!("sqlite://{}", db.display()),
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
            query_timeout: Duration::from_secs(2),
            read_only,
        };
        let state = qiye_server::connect(config).await.unwrap();
        if let Db::Sqlite(pool) = &state.db {
            let mut c = pool.acquire().await.unwrap();
            sqlx::query("PRAGMA foreign_keys=OFF")
                .execute(&mut *c)
                .await
                .unwrap();
            sqlx::raw_sql(include_str!("../contracts/fixture.txt"))
                .execute(&mut *c)
                .await
                .unwrap();
            sqlx::query("PRAGMA foreign_keys=ON")
                .execute(&mut *c)
                .await
                .unwrap();
        }
        let router = qiye_server::router(state.clone());
        let r = router
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
        assert_eq!(r.status(), StatusCode::OK);
        let cookie = r.headers()["set-cookie"]
            .to_str()
            .unwrap()
            .split(';')
            .next()
            .unwrap()
            .to_owned();
        Self {
            router,
            state,
            cookie,
            _temp: temp,
        }
    }
    async fn request(&self, method: &str, path: &str, body: Option<Value>) -> (StatusCode, Value) {
        let mut req = Request::builder()
            .method(method)
            .uri(path)
            .header("cookie", &self.cookie);
        if body.is_some() {
            req = req.header("content-type", "application/json");
        }
        let r = self
            .router
            .clone()
            .oneshot(
                req.body(
                    body.map(|v| Body::from(v.to_string()))
                        .unwrap_or_else(Body::empty),
                )
                .unwrap(),
            )
            .await
            .unwrap();
        let status = r.status();
        let bytes = r.into_body().collect().await.unwrap().to_bytes();
        (
            status,
            serde_json::from_slice(&bytes).unwrap_or_else(|_| json!({"binary_size":bytes.len()})),
        )
    }
}
fn normalize(v: &mut Value) {
    match v {
        Value::Array(a) => {
            for x in a {
                normalize(x);
            }
        }
        Value::Object(o) => {
            if let Some(Value::Array(tags)) = o.get_mut("tags") {
                tags.sort_by_key(|t| format!("{}:{}", t["id"], t["origin"]));
            }
            for v in o.values_mut() {
                normalize(v);
            }
        }
        _ => (),
    }
}

#[tokio::test]
async fn python_response_contracts() {
    let app = TestApp::new(true).await;
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("../contracts/responses.json")).unwrap();
    for case in cases {
        let path = case["path"].as_str().unwrap();
        let (status, mut actual) = app.request("GET", path, None).await;
        assert_eq!(status, StatusCode::OK, "{path}: {actual}");
        let mut expected = case["body"].clone();
        normalize(&mut actual);
        normalize(&mut expected);
        assert_eq!(actual, expected, "Python compatibility: {path}");
    }
}
#[tokio::test]
async fn authentication_origin_and_validation_boundaries() {
    let app = TestApp::new(false).await;
    let r = app
        .router
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/items")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(r.status(), 401);
    let r = app
        .router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/auth/login")
                .header("origin", "https://untrusted.example")
                .header("content-type", "application/json")
                .body(Body::from(json!({"password":"local-library"}).to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(r.status(), 403);
    let r = app
        .router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/auth/login")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"password": {"secret": "never-echo-me"}}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(r.status(), 422);
    let body = r.into_body().collect().await.unwrap().to_bytes();
    assert!(!String::from_utf8_lossy(&body).contains("never-echo-me"));
    let (status, body) = app.request("GET", "/api/network/managed", None).await;
    assert_eq!(status, 200);
    assert_eq!(body["enabled"], false);
    let (_, migration) = app.request("GET", "/api/rust/migration", None).await;
    assert_eq!(migration["remaining"], json!([]));
}
#[tokio::test]
async fn read_only_validation_does_not_write_user_state() {
    let app = TestApp::new(true).await;
    for (method, path, body) in [
        ("PUT", "/api/items/item-1/score", json!({"score":5})),
        (
            "PUT",
            "/api/items/item-1/favorite",
            json!({"favorite":true}),
        ),
        ("POST", "/api/items/item-1/visit", json!({})),
    ] {
        assert_eq!(app.request(method, path, Some(body)).await.0, 409);
    }
    assert_eq!(
        app.state
            .db
            .count("SELECT user_score FROM items WHERE id='item-1'", &[])
            .await
            .unwrap(),
        0
    );
}
#[tokio::test]
async fn favorites_and_scores_are_independent_and_group_changes_are_atomic() {
    let app = TestApp::new(false).await;
    assert_eq!(
        app.request("PUT", "/api/items/item-1/score", Some(json!({"score":4})))
            .await
            .0,
        200
    );
    let (s, v) = app
        .request(
            "PUT",
            "/api/items/item-1/favorite",
            Some(json!({"favorite":false})),
        )
        .await;
    assert_eq!(s, 200);
    assert_eq!(v["user_score"], 4);
    let (s, v) = app
        .request(
            "PUT",
            "/api/items/item-1/favorite",
            Some(json!({"favorite":true,"group_ids":["group-1"]})),
        )
        .await;
    assert_eq!(s, 200);
    assert_eq!(v["user_score"], 4);
    assert_eq!(
        app.request(
            "PUT",
            "/api/items/item-1/favorite",
            Some(json!({"favorite":true,"group_ids":["does-not-exist"]}))
        )
        .await
        .0,
        400
    );
    let (_, v) = app.request("GET", "/api/items/item-1", None).await;
    assert_eq!(v["favorite_group_ids"], json!(["group-1"]));
    assert_eq!(v["user_score"], 4);
    assert_eq!(
        app.request(
            "PUT",
            "/api/items/item-1/score",
            Some(json!({"score":true}))
        )
        .await
        .0,
        422
    );
    assert_eq!(
        app.request("PUT", "/api/items/item-1/score", Some(json!({"score":6})))
            .await
            .0,
        422
    );
    assert_eq!(
        app.request("DELETE", "/api/favorite-groups/group-1", None)
            .await
            .0,
        200
    );
    let (_, v) = app.request("GET", "/api/items/item-1", None).await;
    assert_eq!(v["is_favorite"], true);
    assert_eq!(v["favorite_group_ids"], json!([]));
}
#[tokio::test]
async fn pairing_is_one_time_devices_are_scoped_and_revocation_is_immediate() {
    let app = TestApp::new(false).await;
    let (s, v) = app
        .request(
            "POST",
            "/api/devices/pairing",
            Some(json!({"server_url":"http://192.168.1.22:8800"})),
        )
        .await;
    assert_eq!(s, 200, "{v}");
    let qr: Value = serde_json::from_str(v["qr_payload"].as_str().unwrap()).unwrap();
    let data = json!({"pairing_token":qr["pairing_token"],"device_name":"测试手机"});
    let (s, device) = app
        .request("POST", "/api/devices/pairing/exchange", Some(data.clone()))
        .await;
    assert_eq!(s, 200, "{device}");
    assert_eq!(
        app.request("POST", "/api/devices/pairing/exchange", Some(data))
            .await
            .0,
        400
    );
    let token = device["access_token"].as_str().unwrap();
    for (path, expected) in [
        ("/api/items", 200),
        ("/api/devices", 403),
        ("/api/rust/migration", 403),
    ] {
        let r = app
            .router
            .clone()
            .oneshot(
                Request::builder()
                    .uri(path)
                    .header("authorization", format!("Bearer {token}"))
                    .header("cookie", &app.cookie)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(r.status().as_u16(), expected);
    }
    let persisted = app
        .state
        .db
        .one(
            "SELECT token_hash FROM device_sessions WHERE id=?",
            &[device["device_id"].clone()],
        )
        .await
        .unwrap()
        .unwrap();
    assert_ne!(persisted["token_hash"], token);
    assert_eq!(persisted["token_hash"].as_str().unwrap().len(), 64);
    assert_eq!(
        app.request(
            "DELETE",
            &format!("/api/devices/{}", device["device_id"].as_str().unwrap()),
            None
        )
        .await
        .0,
        200
    );
    let r = app
        .router
        .oneshot(
            Request::builder()
                .uri("/api/items")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(r.status(), 401);
}
#[tokio::test]
async fn asset_stream_ranges_head_storage_markers_and_traversal() {
    let app = TestApp::new(true).await;
    let r = app
        .router
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/assets/asset-1/content")
                .header("cookie", &app.cookie)
                .header("range", "bytes=0-7")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(r.status(), 206);
    assert_eq!(r.into_body().collect().await.unwrap().to_bytes().len(), 8);
    let r = app
        .router
        .clone()
        .oneshot(
            Request::builder()
                .method("HEAD")
                .uri("/api/assets/asset-1/content")
                .header("cookie", &app.cookie)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    assert!(r.into_body().collect().await.unwrap().to_bytes().is_empty());
    app.state
        .db
        .execute(
            "UPDATE assets SET relative_path='../outside' WHERE id='asset-1'",
            &[],
        )
        .await
        .unwrap();
    assert_eq!(
        app.request("GET", "/api/assets/asset-1/content", None)
            .await
            .0,
        400
    );
    std::fs::remove_file(app.state.config.storage_root.join(".library-storage")).unwrap();
    assert_eq!(app.request("GET", "/api/health", None).await.0, 503);
    assert_eq!(app.request("GET", "/api/items", None).await.0, 200);
}
#[tokio::test]
async fn novel_progress_validates_sections_without_counting_inline_illustrations() {
    let app = TestApp::new(false).await;
    for (body, expected) in [
        (json!({"page":0,"section":1,"offset":0.8}), 200),
        (json!({"page":0,"section":5}), 400),
        (json!({"page":1,"section":0}), 400),
        (json!({"page":0,"offset":-0.1}), 422),
    ] {
        let (status, v) = app
            .request("PUT", "/api/items/item-3/progress", Some(body))
            .await;
        assert_eq!(status.as_u16(), expected, "{v}");
    }
    let (_, v) = app.request("GET", "/api/items/item-3", None).await;
    assert_eq!(v["progress"], json!({"page":0,"section":1,"offset":0.8}));
}
#[tokio::test]
async fn malformed_filters_and_sql_wildcards_cannot_expand_queries() {
    let app = TestApp::new(true).await;
    for path in [
        "/api/items?page=0",
        "/api/items?page_size=101",
        "/api/items?tag_mode=invalid",
        "/api/items?favorite=maybe",
        "/api/items?tag_ids=1234567890123456789012345678901234567",
    ] {
        assert_eq!(app.request("GET", path, None).await.0, 422, "{path}");
    }
    let (_, v) = app
        .request("GET", "/api/items?q=%27%20OR%201%3D1--", None)
        .await;
    assert_eq!(v["total"], 0);
}
#[tokio::test]
async fn duplicate_group_names_roll_back_and_names_are_normalized() {
    let app = TestApp::new(false).await;
    assert_eq!(
        app.request(
            "POST",
            "/api/favorite-groups",
            Some(json!({"name":"  想看 "}))
        )
        .await
        .0,
        409
    );
    let (s, v) = app
        .request(
            "POST",
            "/api/favorite-groups",
            Some(json!({"name":"  新分组 "})),
        )
        .await;
    assert_eq!(s, 200);
    assert_eq!(v["name"], "新分组");
    assert_eq!(
        app.request("POST", "/api/favorite-groups", Some(json!({"name":"\n"})))
            .await
            .0,
        422
    );
}
#[tokio::test]
async fn queue_controls_keep_independent_lanes_and_pause_history() {
    let app = TestApp::new(false).await;
    assert_eq!(
        app.request(
            "PUT",
            "/api/queue/lanes/manual_download",
            Some(json!({"concurrency":3}))
        )
        .await
        .0,
        200
    );
    assert_eq!(
        app.request(
            "PUT",
            "/api/queue/settings",
            Some(json!({"paused":true,"concurrency":5,"pause_reason":"maintenance"}))
        )
        .await
        .0,
        200
    );
    let (_, v) = app.request("GET", "/api/queue/status", None).await;
    let lanes = v["lanes"].as_array().unwrap();
    let manual = lanes.iter().find(|l| l["id"] == "manual_download").unwrap();
    let download = lanes.iter().find(|l| l["id"] == "download").unwrap();
    assert_eq!(manual["concurrency"], 3);
    assert_eq!(manual["blocked_by_master"], false);
    assert_eq!(download["concurrency"], 5);
    assert_eq!(download["blocked_by_master"], true);
    let before = app
        .state
        .db
        .one("SELECT lanes FROM queue_control WHERE id=1", &[])
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        app.request("PUT", "/api/queue/settings", Some(json!({"paused":true})))
            .await
            .0,
        200
    );
    let after = app
        .state
        .db
        .one("SELECT lanes FROM queue_control WHERE id=1", &[])
        .await
        .unwrap()
        .unwrap();
    assert_eq!(before["lanes"], after["lanes"]);
    assert_eq!(
        app.request("PUT", "/api/queue/settings", Some(json!({"concurrency":9})))
            .await
            .0,
        422
    );
    assert_eq!(
        app.request(
            "PUT",
            "/api/queue/settings",
            Some(json!({"min_interval_seconds":50,"max_interval_seconds":3}))
        )
        .await
        .0,
        400
    );
}
#[tokio::test]
async fn source_switches_persist_independently_and_validate_writes() {
    let app = TestApp::new(false).await;
    let (_, defaults) = app.request("GET", "/api/queue/sources", None).await;
    assert_eq!(defaults["items"].as_array().unwrap().len(), 5);
    assert!(
        defaults["items"]
            .as_array()
            .unwrap()
            .iter()
            .all(|v| v["enabled"] == true)
    );
    let (_, before) = app.request("GET", "/api/queue/settings", None).await;
    for id in ["pixiv", "kemono"] {
        let (status, value) = app
            .request(
                "PUT",
                &format!("/api/queue/sources/{id}"),
                Some(json!({"enabled":false})),
            )
            .await;
        assert_eq!(status, 200, "{value}");
        assert_eq!(value["enabled"], false);
        assert!(value["changed_at"].is_string());
    }
    assert_eq!(
        app.request("GET", "/api/queue/settings", None).await.1,
        before
    );
    // A new process has no per-source memory state to recover: read the same
    // database through a fresh connection and router.
    let restarted = qiye_server::connect(app.state.config.clone())
        .await
        .unwrap();
    let response = qiye_server::router(restarted)
        .oneshot(
            Request::builder()
                .uri("/api/queue/sources/pixiv")
                .header("cookie", &app.cookie)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let value: Value =
        serde_json::from_slice(&response.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert_eq!(value["enabled"], false);
    assert_eq!(
        app.request("GET", "/api/queue/sources/jm", None).await.1["enabled"],
        true
    );
    assert_eq!(
        app.request(
            "PUT",
            "/api/queue/sources/local",
            Some(json!({"enabled":false}))
        )
        .await
        .0,
        404
    );
    assert_eq!(
        app.request(
            "PUT",
            "/api/queue/sources/pixiv",
            Some(json!({"enabled":"false"}))
        )
        .await
        .0,
        422
    );
    assert_eq!(
        app.request(
            "PUT",
            "/api/queue/sources/pixiv",
            Some(json!({"enabled":false,"paused":true}))
        )
        .await
        .0,
        422
    );
    let (_, catalog) = app.request("GET", "/api/mobile-admin/catalog", None).await;
    assert!(
        catalog["actions"]
            .as_array()
            .unwrap()
            .iter()
            .any(|a| a["method"] == "PUT" && a["path"] == "/queue/sources/{platform}")
    );
    let read_only = TestApp::new(true).await;
    assert_eq!(
        read_only
            .request(
                "PUT",
                "/api/queue/sources/pixiv",
                Some(json!({"enabled":false}))
            )
            .await
            .0,
        409
    );
}
#[tokio::test]
async fn disabled_sources_do_not_starve_other_sources_or_block_manual_and_retry_jobs() {
    use qiye_server::tasks;
    use std::sync::Arc;
    use tokio_util::sync::CancellationToken;
    let app = TestApp::new(false).await;
    let account = qiye_server::accounts::save(
        &app.state,
        "pixiv",
        "fixture",
        &json!({"refresh_token":"synthetic-only-refresh-token"}),
        false,
    )
    .await
    .unwrap();
    qiye_server::secrets::put(
        &app.state,
        qiye_server::accounts::ROLE_KEY,
        &json!({"enabled":false,"retry_account_ids":[account["id"]]}),
    )
    .await
    .unwrap();
    app.state.db.execute("DELETE FROM jobs", &[]).await.unwrap();
    let mut tx = app.state.db.begin().await.unwrap();
    for n in 0..80 {
        tasks::enqueue_tx(
            &app.state,
            &mut tx,
            "import",
            &json!({"platform":"ehentai","remote_id":format!("{n}/test"),"download":false}),
        )
        .await
        .unwrap();
    }
    let active = tasks::enqueue_tx(
        &app.state,
        &mut tx,
        "import",
        &json!({"platform":"kemono","remote_id":"patreon/1/2","download":false}),
    )
    .await
    .unwrap();
    tasks::enqueue_tx(
        &app.state,
        &mut tx,
        "author_profile",
        &json!({"author_id":"77"}),
    )
    .await
    .unwrap();
    tasks::enqueue_tx(
        &app.state,
        &mut tx,
        "import",
        &json!({"platform":"pixiv","remote_id":"5","download_origin":"manual"}),
    )
    .await
    .unwrap();
    tasks::enqueue_tx(
        &app.state,
        &mut tx,
        "import",
        &json!({"platform":"pixiv","remote_id":"6","retry_queue":true}),
    )
    .await
    .unwrap();
    tasks::enqueue_tx(&app.state, &mut tx, "ai", &json!({"item_id":"item-1"}))
        .await
        .unwrap();
    tx.commit().await.unwrap();
    for id in ["ehentai", "pixiv"] {
        assert_eq!(
            app.request(
                "PUT",
                &format!("/api/queue/sources/{id}"),
                Some(json!({"enabled":false}))
            )
            .await
            .0,
            200
        );
    }
    let claim = tasks::claim(&app.state, "details").await.unwrap().unwrap();
    assert_eq!(
        claim.job["id"], active["id"],
        "disabled jobs must be filtered before the 64-candidate limit"
    );
    tasks::execute(
        app.state.clone(),
        claim,
        Arc::new(CompletedExecutor),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert!(tasks::claim(&app.state, "details").await.unwrap().is_none());
    assert!(
        tasks::claim(&app.state, "checks").await.unwrap().is_none(),
        "author tasks without a platform field still belong to Pixiv"
    );
    let (_, status) = app.request("GET", "/api/queue/status", None).await;
    let checks = status["lanes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["id"] == "checks")
        .unwrap();
    assert_eq!(checks["diagnostic"]["code"], "source_paused");
    assert_eq!(checks["blocked_sources"][0]["id"], "pixiv");
    app.request("PUT", "/api/queue/settings", Some(json!({"paused":true})))
        .await;
    for lane in ["manual_download", "retry", "ai"] {
        let claim = tasks::claim(&app.state, lane).await.unwrap().expect(lane);
        tasks::execute(
            app.state.clone(),
            claim.clone(),
            Arc::new(CompletedExecutor),
            CancellationToken::new(),
        )
        .await
        .unwrap();
        assert_eq!(
            app.state
                .db
                .one(
                    "SELECT status FROM jobs WHERE id=?",
                    &[claim.job["id"].clone()]
                )
                .await
                .unwrap()
                .unwrap()["status"],
            "completed"
        );
    }
    app.request("PUT", "/api/queue/settings", Some(json!({"paused":false})))
        .await;
    app.request(
        "PUT",
        "/api/queue/sources/ehentai",
        Some(json!({"enabled":true})),
    )
    .await;
    let claim = tasks::claim(&app.state, "details").await.unwrap().unwrap();
    let ctx = tasks::Context::new(app.state.clone(), &claim, CancellationToken::new());
    ctx.checkpoint(Some(25.0)).await.unwrap();
    app.request(
        "PUT",
        "/api/queue/sources/ehentai",
        Some(json!({"enabled":false})),
    )
    .await;
    assert!(matches!(
        ctx.checkpoint(None).await,
        Err(tasks::TaskError::Paused)
    ));
    tasks::execute(
        app.state.clone(),
        claim.clone(),
        Arc::new(CompletedExecutor),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    let row = app
        .state
        .db
        .one(
            "SELECT status,error,worker_id FROM jobs WHERE id=?",
            &[claim.job["id"].clone()],
        )
        .await
        .unwrap()
        .unwrap();
    assert_eq!(row["status"], "queued");
    assert!(row["error"].is_null());
    assert!(row["worker_id"].is_null());
    app.request(
        "PUT",
        "/api/queue/sources/ehentai",
        Some(json!({"enabled":true})),
    )
    .await;
    assert!(tasks::claim(&app.state, "details").await.unwrap().is_some());
}
#[tokio::test]
async fn source_pause_holds_subscription_scheduling_until_resumed() {
    let app = TestApp::new(false).await;
    app.state.db.execute("DELETE FROM jobs", &[]).await.unwrap();
    let (status, sub) = app
        .request(
            "POST",
            "/api/subscriptions",
            Some(
                json!({"platform":"pixiv","target_type":"author","target":"77","label":"fixture"}),
            ),
        )
        .await;
    assert_eq!(status, 200, "{sub}");
    app.request(
        "PUT",
        "/api/queue/sources/pixiv",
        Some(json!({"enabled":false})),
    )
    .await;
    qiye_server::acquisition::tick(&app.state).await.unwrap();
    assert_eq!(
        app.state
            .db
            .count("SELECT COUNT(*) FROM jobs", &[])
            .await
            .unwrap(),
        0
    );
    assert!(
        app.state
            .db
            .one(
                "SELECT next_check_at FROM subscriptions WHERE id=?",
                &[sub["id"].clone()]
            )
            .await
            .unwrap()
            .unwrap()["next_check_at"]
            .is_null()
    );
    assert_eq!(
        app.request("GET", "/api/pixiv/authors/77", None).await.1["refresh_paused"],
        true
    );
    app.request(
        "PUT",
        "/api/queue/sources/pixiv",
        Some(json!({"enabled":true})),
    )
    .await;
    qiye_server::acquisition::tick(&app.state).await.unwrap();
    assert_eq!(
        app.state
            .db
            .count("SELECT COUNT(*) FROM jobs WHERE kind='source_sync'", &[])
            .await
            .unwrap(),
        1
    );
}

#[tokio::test]
async fn thumbnails_merge_concurrent_requests_without_modifying_the_nas() {
    let app = TestApp::new(true).await;
    let (a, b) = tokio::join!(
        app.request("GET", "/api/assets/asset-1/thumbnail", None),
        app.request("GET", "/api/assets/asset-1/thumbnail", None)
    );
    assert_eq!(a.0, 200, "{}", a.1);
    assert_eq!(b.0, 200, "{}", b.1);
    assert_eq!(a.1, b.1);
    assert_eq!(
        std::fs::read_dir(app.state.thumbnail_cache.path())
            .unwrap()
            .count(),
        1
    );
    assert!(!app.state.config.storage_root.join("thumbnails").exists());
}
#[tokio::test]
async fn series_reordering_is_transactional_and_revisions_prevent_lost_updates() {
    let app = TestApp::new(false).await;
    let original = json!({"title":"上下集","revision":1,"entries":[{"item_id":"item-3","label":"下"},{"item_id":"item-1","label":"上"}]});
    let (s, v) = app
        .request("PUT", "/api/series/series-1", Some(original.clone()))
        .await;
    assert_eq!(s, 200, "{v}");
    assert_eq!(v["revision"], 2);
    assert_eq!(v["entries"][0]["item_id"], "item-3");
    assert_eq!(
        app.request("PUT", "/api/series/series-1", Some(original))
            .await
            .0,
        409
    );
    assert_eq!(
        app.request(
            "PUT",
            "/api/series/series-2",
            Some(json!({"title":"官方","revision":2,"entries":[]}))
        )
        .await
        .0,
        409
    );
    assert_eq!(app.request("POST","/api/series",Some(json!({"title":"重复","kind":"manga","entries":[{"item_id":"item-1"},{"item_id":"item-1"}]}))).await.0,400);
    assert_eq!(
        app.request(
            "POST",
            "/api/series",
            Some(json!({"title":"不存在","kind":"manga","entries":[{"item_id":"missing"}]}))
        )
        .await
        .0,
        404
    );
    assert_eq!(
        app.state
            .db
            .count("SELECT COUNT(*) FROM series", &[])
            .await
            .unwrap(),
        2
    );
}
#[tokio::test]
async fn encrypted_account_roles_preserve_permissions_and_remove_deleted_accounts() {
    let app = TestApp::new(false).await;
    let secret = &app.state.config.secret;
    let old: Value = serde_json::from_str(include_str!("../contracts/legacy-fernet.json")).unwrap();
    assert_eq!(
        qiye_server::secrets::decrypt(secret, old["encrypted"].as_str().unwrap()).unwrap(),
        old["plain"]
    );
    assert!(
        qiye_server::secrets::decrypt("different-test-key", old["encrypted"].as_str().unwrap())
            .is_err()
    );
    let mut ids = vec![];
    for i in 0..3 {
        let(s,v)=app.request("POST","/api/sources/pixiv/accounts",Some(json!({"label":format!("测试账号{i}"),"credentials":{"refresh_token":format!("synthetic-session-{i}"),"user_id":format!("{i}")}}))).await;
        assert_eq!(s, 200, "{v}");
        assert!(v.get("credentials").is_none());
        ids.push(v["id"].as_str().unwrap().to_owned());
    }
    let config = json!({"enabled":true,"main_account_id":ids[0],"content_account_ids":[ids[1]],"manual_account_ids":[ids[2]],"detail_account_ids":[ids[1]],"check_account_ids":[ids[2]],"retry_account_ids":[ids[1]],"account_limits":{(ids[1].clone()):3}});
    let (s, v) = app
        .request("PUT", "/api/sources/pixiv/account-roles", Some(config))
        .await;
    assert_eq!(s, 200, "{v}");
    assert_eq!(v["retry_account_ids"], json!([ids[1]]));
    let (s, v) = app
        .request(
            "PUT",
            "/api/sources/pixiv/account-roles",
            Some(json!({"enabled":true,"main_account_id":ids[0],"content_account_ids":[ids[1]]})),
        )
        .await;
    assert_eq!(s, 200, "{v}");
    assert_eq!(v["manual_account_ids"], json!([ids[2]]));
    assert_eq!(v["retry_account_ids"], json!([ids[1]]));
    assert_eq!(v["account_limits"][&ids[1]], 3);
    let saved = app
        .state
        .db
        .one(
            "SELECT value_encrypted FROM network_settings WHERE `key`='pixiv_account_roles'",
            &[],
        )
        .await
        .unwrap()
        .unwrap();
    assert!(!saved["value_encrypted"].as_str().unwrap().contains(&ids[0]));
    assert_eq!(
        app.request(
            "DELETE",
            &format!("/api/sources/pixiv/accounts/{}", ids[2]),
            None
        )
        .await
        .0,
        200
    );
    let (_, v) = app
        .request("GET", "/api/sources/pixiv/account-roles", None)
        .await;
    assert_eq!(v["download_accounts"].as_array().unwrap().len(), 2);
    assert!(
        v["download_accounts"]
            .as_array()
            .unwrap()
            .iter()
            .all(|a| a["id"] != ids[2])
    );
    assert_eq!(v["missing_account_ids"], json!([ids[2]]));
}
#[test]
fn assignment_respects_per_account_capacity_original_roles_and_retry_permissions() {
    use qiye_server::accounts::{Roles, choose_assignment, validate_retry};
    use std::collections::{HashMap, HashSet};
    let roles:Roles=serde_json::from_value(json!({"enabled":true,"main_account_id":"main","content_account_ids":["image"],"manual_account_ids":["manual"],"detail_account_ids":["text"],"check_account_ids":["author"],"retry_account_ids":["image","text"],"account_limits":{"image":2,"text":3,"manual":1}})).unwrap();
    let roles = roles.normalize().unwrap();
    let records: HashSet<_> = ["main", "image", "manual", "text", "author"]
        .into_iter()
        .map(str::to_owned)
        .collect();
    let mut cursor = 0;
    let job =
        json!({"kind":"import","payload":{"platform":"pixiv","remote_id":"123","download":true}});
    let p = choose_assignment(&roles, &job, &records, &HashMap::new(), &mut cursor, None).unwrap();
    assert_eq!(p["download_account_id"], "image");
    assert_eq!(p["details_account_id"], "text");
    assert!(
        choose_assignment(
            &roles,
            &job,
            &records,
            &HashMap::from([("image".to_owned(), 2)]),
            &mut cursor,
            None
        )
        .is_none()
    );
    let novel =
        json!({"kind":"import_content","payload":{"platform":"pixiv","remote_id":"novel:42"}});
    let p =
        choose_assignment(&roles, &novel, &records, &HashMap::new(), &mut cursor, None).unwrap();
    assert!(p.get("download_account_id").is_none());
    assert_eq!(p["details_account_id"], "text");
    let manual = json!({"kind":"import","payload":{"platform":"pixiv","remote_id":"123","download_origin":"manual","retry_queue":true}});
    let p = choose_assignment(
        &roles,
        &manual,
        &records,
        &HashMap::new(),
        &mut cursor,
        None,
    )
    .unwrap();
    assert!(
        validate_retry(
            &roles,
            &json!({"kind":"import","payload":p}),
            &records,
            None
        )
        .is_err()
    );
    let mut retry = job;
    retry["payload"]["retry_queue"] = json!(true);
    let p =
        choose_assignment(&roles, &retry, &records, &HashMap::new(), &mut cursor, None).unwrap();
    assert!(
        validate_retry(
            &roles,
            &json!({"kind":"import","payload":p}),
            &records,
            None
        )
        .is_ok()
    );
}
#[tokio::test]
async fn successful_logins_do_not_consume_the_failed_password_budget() {
    let app = TestApp::new(false).await;
    for _ in 0..15 {
        assert_eq!(
            app.request(
                "POST",
                "/api/auth/login",
                Some(json!({"password":"local-library"}))
            )
            .await
            .0,
            200
        );
    }
    for _ in 0..12 {
        assert_eq!(
            app.request(
                "POST",
                "/api/auth/login",
                Some(json!({"password":"synthetic-wrong-password"}))
            )
            .await
            .0,
            401
        );
    }
    assert_eq!(
        app.request(
            "POST",
            "/api/auth/login",
            Some(json!({"password":"local-library"}))
        )
        .await
        .0,
        429
    );
}
#[tokio::test]
async fn proxy_secrets_are_retained_but_never_returned_and_endpoints_reject_private_hosts() {
    let app = TestApp::new(false).await;
    let(s,v)=app.request("PUT","/api/network/proxy",Some(json!({"enabled":true,"proxy_url":"http://127.0.0.1:17890","proxy_username":"test-user","proxy_password":"synthetic-proxy-password","controller_url":"http://127.0.0.1:19090","controller_secret":"synthetic-controller-key"}))).await;
    assert_eq!(s, 200, "{v}");
    assert_eq!(v["has_proxy_password"], true);
    assert!(v.get("proxy_password").is_none());
    assert!(!v.to_string().contains("synthetic-"));
    let(s,v)=app.request("PUT","/api/network/proxy",Some(json!({"enabled":false,"proxy_url":"http://127.0.0.1:17890","proxy_username":"test-user","controller_url":"http://127.0.0.1:19090"}))).await;
    assert_eq!(s, 200, "{v}");
    assert_eq!(v["has_proxy_password"], true);
    assert_eq!(v["has_controller_secret"], true);
    let (s, v) = app
        .request(
            "PUT",
            "/api/platform-endpoints/pixiv",
            Some(json!({"enabled":false,"custom_url":"https://example.com/pixiv"})),
        )
        .await;
    assert_eq!(s, 200, "{v}");
    assert_eq!(v["effective_url"], "https://app-api.pixiv.net");
    assert_eq!(v["custom_url"], "https://example.com/pixiv");
    for url in [
        "http://example.com/api",
        "https://127.0.0.1/api",
        "https://example.com/../api",
        "https://name:secret@example.com/api",
        "https://example.com/%2e%2e/api",
    ] {
        assert_eq!(
            app.request(
                "PUT",
                "/api/platform-endpoints/pixiv",
                Some(json!({"enabled":false,"custom_url":url}))
            )
            .await
            .0,
            400,
            "{url}"
        );
    }
    for ip in [
        "127.0.0.1",
        "192.168.100.100",
        "100.64.1.2",
        "198.18.0.1",
        "::1",
        "::ffff:192.168.100.1",
        "fd00::1",
        "2001:db8::1",
    ] {
        assert!(
            !qiye_server::network::public_ip(ip.parse().unwrap()),
            "{ip}"
        );
    }
}
struct CompletedExecutor;
impl qiye_server::tasks::Executor for CompletedExecutor {
    fn execute(
        &self,
        _: qiye_server::tasks::Context,
        _: Value,
    ) -> std::pin::Pin<
        Box<dyn std::future::Future<Output = qiye_server::tasks::TaskResult<Value>> + Send>,
    > {
        Box::pin(async { Ok(json!({"synthetic":true})) })
    }
}
#[tokio::test]
async fn manual_dispatch_ignores_master_pause_and_owner_guards_prevent_stale_writes() {
    use qiye_server::tasks;
    use std::sync::Arc;
    use tokio_util::sync::CancellationToken;
    let app = TestApp::new(false).await;
    assert_eq!(
        app.request("PUT", "/api/queue/settings", Some(json!({"paused":true})))
            .await
            .0,
        200
    );
    assert!(tasks::claim(&app.state, "details").await.unwrap().is_none());
    let claim = tasks::claim(&app.state, "manual_download")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(claim.job["id"], "job-2");
    assert!(
        tasks::claim(&app.state, "manual_download")
            .await
            .unwrap()
            .is_none()
    );
    tasks::execute(
        app.state.clone(),
        claim.clone(),
        Arc::new(CompletedExecutor),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    let row = app
        .state
        .db
        .one("SELECT * FROM jobs WHERE id='job-2'", &[])
        .await
        .unwrap()
        .unwrap();
    assert_eq!(row["status"], "completed");
    assert_eq!(
        app.request("PUT", "/api/queue/settings", Some(json!({"paused":false})))
            .await
            .0,
        200
    );
    let claim = tasks::claim(&app.state, "details").await.unwrap().unwrap();
    let ctx = tasks::Context::new(app.state.clone(), &claim, CancellationToken::new());
    app.state
        .db
        .execute(
            "UPDATE jobs SET worker_id='replacement-owner' WHERE id=?",
            &[claim.job["id"].clone()],
        )
        .await
        .unwrap();
    assert!(matches!(
        ctx.checkpoint(Some(99.0)).await,
        Err(tasks::TaskError::Cancelled)
    ));
    let row = app
        .state
        .db
        .one(
            "SELECT progress,worker_id FROM jobs WHERE id=?",
            &[claim.job["id"].clone()],
        )
        .await
        .unwrap()
        .unwrap();
    assert_eq!(row["progress"], 0.0);
    assert_eq!(row["worker_id"], "replacement-owner");
}
#[tokio::test]
async fn import_enqueue_merges_duplicate_requests_and_promotes_waiting_manual_work() {
    let app = TestApp::new(false).await;
    let p =
        json!({"platform":"pixiv","remote_id":"123","download":true,"download_origin":"manual"});
    let mut tx = app.state.db.begin().await.unwrap();
    let j = qiye_server::tasks::enqueue_import_tx(&app.state, &mut tx, p.clone())
        .await
        .unwrap();
    tx.commit().await.unwrap();
    assert_eq!(j["id"], "job-1");
    assert_eq!(j["payload"]["download_origin"], "manual");
    let mut tx = app.state.db.begin().await.unwrap();
    let again = qiye_server::tasks::enqueue_import_tx(&app.state, &mut tx, p)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    assert_eq!(again["id"], j["id"]);
    assert_eq!(
        app.state
            .db
            .count("SELECT COUNT(*) FROM jobs", &[])
            .await
            .unwrap(),
        3
    );
    let (s, v) = app
        .request("POST", "/api/jobs/job-1/cancel", Some(json!({})))
        .await;
    assert_eq!(s, 200, "{v}");
    assert_eq!(v["status"], "cancelled");
    let (s, v) = app
        .request("POST", "/api/jobs/job-1/retry", Some(json!({})))
        .await;
    assert_eq!(s, 200, "{v}");
    assert_eq!(v["queue"], "retry");
    assert_eq!(v["original_queue"], "manual_download");
}
#[test]
fn production_rejects_weak_settings() {
    // No production environment variables or private configuration are read.
    let cfg = Config {
        database_url: "sqlite://fixture".into(),
        storage_root: PathBuf::new(),
        import_root: PathBuf::new(),
        frontend: PathBuf::new(),
        password: "short".into(),
        secret: "weak".into(),
        production: true,
        secure_cookie: false,
        require_marker: true,
        allowed_origins: vec![],
        bind: "127.0.0.1:8801".parse().unwrap(),
        pool_size: 4,
        query_timeout: Duration::from_secs(2),
        read_only: true,
    };
    assert!(cfg.validate().is_err());
}
