use qiye_server::{
    content_store,
    platforms::{ehentai, jm, kemono, picacg, pixiv},
};
use serde_json::json;
#[test]
fn subscriptions_accept_nodes_but_reject_local_files_aliases_and_unsafe_urls() {
    use qiye_server::proxy_subscription as p;
    let config=b"proxies:\n - name: test\n   type: ss\n   server: example.com\n   port: 443\n   cipher: aes-128-gcm\n   password: fixture\nrules: ['MATCH,DIRECT']\n";
    assert_eq!(p::parse(config).unwrap().len(), 1);
    let answer = json!({"Status":0,"Question":[{"name":"example.com.","type":1}],"Answer":[{"name":"example.com.","type":5,"data":"cdn.example.com."},{"name":"cdn.example.com.","type":1,"data":"8.8.8.8"}]});
    assert_eq!(p::doh_answers(&answer, "example.com", 1).unwrap().len(), 1);
    let mut private = answer.clone();
    private["Answer"][1]["data"] = json!("127.0.0.1");
    assert!(p::doh_answers(&private, "example.com", 1).is_err());
    assert!(p::doh_answers(&answer, "different.example.com", 1).is_err());
    assert!(p::parse(b"proxies: &nodes [*nodes]").is_err());
    assert!(p::sanitize(json!({"name":"bad","type":"trojan","server":"example.com","port":443,"certificate":"/etc/passwd"})).is_err());
    assert!(p::sanitize(json!({"name":"bad","type":"ss","server":"example.com","port":443,"ws-opts":{"path-file":"/secret"}})).is_err());
    let n=p::uri("vless://fixture@example.com:443?security=reality&pbk=key&sid=12&type=ws&path=%2Fsocket#name").unwrap();
    assert_eq!(n["tls"], true);
    assert_eq!(n["ws-opts"]["path"], "/socket");
    assert_eq!(n["reality-opts"]["public-key"], "key");
    assert!(p::uri("vless://fixture@example.com?security=unsupported").is_err());
    assert!(p::uri("trojan://fixture@example.com?ech=secret").is_err());
    assert_eq!(
        p::uri("ss://YWVzLTEyOC1nY206Zml4dHVyZQ@example.com:443#test").unwrap()["password"],
        "fixture"
    );
    for url in [
        "http://example.com/feed",
        "https://127.1/feed",
        "https://user@example.com/feed",
        "https://192.168.1.1/feed",
        "https://example.local/feed",
    ] {
        assert!(p::validate_url(url).is_err(), "{url}");
    }
}
#[test]
fn apk_publication_checks_package_signature_version_and_release_mode() {
    let cert = "ab".repeat(32);
    let sig = format!(
        "Verified using v2 scheme (APK Signature Scheme v2): true\nSigner #1 certificate SHA-256 digest: {cert}\n"
    );
    let metadata = "package: name='app.qiye.qiye_library' versionCode='83' versionName='0.8.3'\nsdkVersion:'23'\n";
    assert_eq!(
        qiye_server::app_updates::parse_metadata(&sig, metadata, &cert).unwrap()["version_code"],
        83
    );
    assert!(
        qiye_server::app_updates::parse_metadata(
            &sig,
            &format!("{metadata}application-debuggable\n"),
            &cert
        )
        .is_err()
    );
    assert!(
        qiye_server::app_updates::parse_metadata(
            &sig,
            &metadata.replace("app.qiye.qiye_library", "another.package"),
            &cert
        )
        .is_err()
    );
    assert!(qiye_server::app_updates::parse_metadata(&sig, metadata, &"cd".repeat(32)).is_err());
    assert!(
        qiye_server::app_updates::parse_metadata(&sig.replace("true", "false"), metadata, &cert)
            .is_err()
    );
}
#[test]
fn public_character_candidates_are_typed_plain_text_with_canonical_links() {
    let v=qiye_server::characters::candidate(&json!({"id":42,"name":{"full":"<b>Name</b>","native":"角色","alternative":["Alias","Alias"]},"description":"<script>secret</script><p>Intro</p>","siteUrl":"http://127.0.0.1/","media":{"nodes":[{"id":12,"type":"MANGA","title":{"native":"作品"}}]}}),None).unwrap();
    assert_eq!(v["source_url"], "https://anilist.co/character/42");
    assert_eq!(v["name"], "Name");
    assert!(!v["description"].as_str().unwrap().contains("secret"));
    assert_eq!(v["aliases"], json!(["Alias"]));
    assert!(
        qiye_server::characters::candidate(&json!({"id":true,"name":{"full":"Invalid"}}), None)
            .is_none()
    );
    let matches=qiye_server::characters::image_matches(&json!({"header":{"status":0},"results":[{"header":{"similarity":"88.25","index_name":"fixture"},"data":{"title":"Work","characters":"A,B","ext_urls":["https://127.1/x","https://www.pixiv.net/artworks/1"]}}]})).unwrap();
    assert_eq!(
        matches[0]["source_urls"],
        json!(["https://www.pixiv.net/artworks/1"])
    );
    assert_eq!(
        qiye_server::secrets::legacy_json(&json!({"中文":"测试😀"})),
        "{\"\\u4e2d\\u6587\": \"\\u6d4b\\u8bd5\\ud83d\\ude00\"}"
    );
}
#[test]
fn canonical_ids_and_platform_boundaries() {
    assert_eq!(
        pixiv::normalize("https://www.pixiv.net/novel/show.php?id=123").unwrap(),
        "novel:123"
    );
    assert!(pixiv::normalize("https://pixiv.net.evil.example/artworks/123").is_err());
    assert_eq!(
        ehentai::normalize("https://e-hentai.org/g/42/abcdef0123/").unwrap(),
        "42:abcdef0123"
    );
    assert_eq!(
        kemono::normalize("https://kemono.cr/fanbox/user/123/post/456", false).unwrap(),
        "fanbox:123:456"
    );
    assert!(kemono::media_path("/../secret").is_err());
    assert!(kemono::media_path("/%2e%2e/secret").is_err());
    assert!(kemono::normalize("discord:123:456", false).is_err());
    assert_eq!(jm::normalize("jm123").unwrap(), "123");
    assert!(jm::normalize("https://example.com/album/123").is_err());
    assert!(picacg::normalize("xyz").is_err());
}
#[test]
fn pixiv_refuses_incomplete_or_repeated_pages_and_parses_novel_json() {
    let mut v = json!({"id":42,"title":"漫画","page_count":2,"user":{"id":12},"type":"manga","meta_pages":[{"image_urls":{"original":"https://i.pximg.net/1.jpg"}},{"image_urls":{"original":"https://i.pximg.net/2.jpg"}}]});
    assert_eq!(pixiv::metadata(&v, false).unwrap().kind, "manga");
    assert_eq!(pixiv::originals(&v).unwrap().len(), 2);
    v["meta_pages"][1] = v["meta_pages"][0].clone();
    assert!(pixiv::originals(&v).is_err());
    v["visible"] = json!(false);
    assert!(pixiv::metadata(&v, false).is_err());
    let parsed = pixiv::extract_novel(
        r#"window.data={novel: {"text":"正文 }, script: test","id":42}, isOwnWork: false};"#,
    )
    .unwrap();
    assert_eq!(parsed["id"], 42);
    assert!(pixiv::extract_novel(r#"novel:{"text":"x"},wrongKey:true"#).is_err());
    assert_eq!(
        pixiv::image_keys("[uploadedimage:12][pixivimage:42-2][uploadedimage:12][pixivimage:42-0]"),
        vec!["uploadedimage:12", "pixivimage:42-2"]
    );
}
#[test]
fn media_credentials_do_not_escape_their_platform() {
    let v = json!({"fileServer":"https://storage1.picacomic.com","path":"/1.jpg"});
    assert_eq!(
        picacg::media_url(&v).unwrap(),
        "https://storage1.picacomic.com/static/1.jpg"
    );
    assert!(
        picacg::media_url(&json!({"fileServer":"https://evil.example","path":"/1.jpg"})).is_err()
    );
    assert!(picacg::media_url(&json!({"path":"../secret"})).is_err());
    let h = picacg::signed("comics/123", "GET", "synthetic-token", "1600000000", "abc").unwrap();
    assert!(h["authorization"].is_sensitive());
    assert_eq!(h["signature"].to_str().unwrap().len(), 64);
    let doc = kemono::document(
        "<p>正文</p><script>secret()</script><img src='/data/a.jpg'><iframe src='https://example.com'></iframe>",
    );
    assert!(doc.text.contains("正文"));
    assert!(!doc.text.contains("secret"));
    assert!(doc.unsupported);
    assert_eq!(doc.media, vec!["/data/a.jpg"]);
    let (links, image) = ehentai::document(
        "<a href='/s/abc/42-1'>1</a><img id='img' src='https://example.com/1.jpg'>",
    );
    assert_eq!(links, vec!["/s/abc/42-1"]);
    assert!(image.is_some());
}
#[test]
fn jm_restoration_preserves_remainder_rows() {
    let mut img = image::RgbImage::new(2, 23);
    for y in 0..23 {
        for x in 0..2 {
            img.put_pixel(x, y, image::Rgb([y as u8, 0, 0]));
        }
    }
    let mut raw = std::io::Cursor::new(vec![]);
    img.write_to(&mut raw, image::ImageFormat::Png).unwrap();
    let restored = jm::restore(&raw.into_inner(), 10).unwrap();
    let out = image::load_from_memory(&restored).unwrap().to_rgb8();
    let expected = [
        18, 19, 20, 21, 22, 16, 17, 14, 15, 12, 13, 10, 11, 8, 9, 6, 7, 4, 5, 2, 3, 0, 1,
    ];
    for (y, expected) in expected.iter().enumerate() {
        assert_eq!(out.get_pixel(0, y as u32).0[0], *expected);
    }
    assert_eq!(jm::segments(220980, 1, "00001"), 0);
    assert_eq!(jm::segments(220980, 230000, "00001"), 10);
}
#[test]
fn video_requires_complete_index_and_payload() {
    let mut video = vec![];
    for (kind, body) in [
        (b"ftyp", b"isom0000".as_slice()),
        (b"moov", b"index".as_slice()),
        (b"mdat", b"payload".as_slice()),
    ] {
        video.extend_from_slice(&((body.len() + 8) as u32).to_be_bytes());
        video.extend_from_slice(kind);
        video.extend_from_slice(body);
    }
    assert_eq!(
        content_store::video_mime("mp4", &video).unwrap(),
        "video/mp4"
    );
    video.pop();
    assert!(content_store::video_mime("mp4", &video).is_err());
    assert!(content_store::video_mime("mp4", b"0000ftypisom0000").is_err());
    assert!(content_store::video_mime("webm", &[0x1a, 0x45, 0xdf, 0xa3]).is_err());
}
