//! Bounded background repair, never executed by a library/list API request.
use crate::{
    AppState,
    db::{json_column, now, truth},
    error::Result,
    queue, tasks,
};
use serde_json::json;
pub async fn tick(s: &AppState, cursor: &mut Option<String>) -> Result<usize> {
    let mut tx = s.db.begin().await?;
    let settings = queue::locked(s, &mut tx).await?;
    if truth(&settings["paused"])
        || queue::lane(&settings, "details")["paused"] == true
        || tx
            .query(
                "SELECT id FROM source_accounts WHERE platform='pixiv' LIMIT 1",
                &[],
            )
            .await?
            .is_empty()
    {
        return Ok(0);
    }
    let auto = format!(
        "kind='import' AND {} IN ('true','1') AND {} IN ('false','0')",
        tasks::json_text(s, "payload", "auto_novel_cover"),
        tasks::json_text(s, "payload", "download")
    );
    let pending = tx
        .query(
            &format!(
                "SELECT COUNT(*) AS n FROM jobs WHERE {auto} AND status IN ('queued','running')"
            ),
            &[],
        )
        .await?
        .remove(0)["n"]
        .as_i64()
        .unwrap_or(0);
    let budget = (20 - pending).max(0) as usize;
    if budget == 0 {
        return Ok(0);
    }
    let rows=tx.query("SELECT id,source_id FROM items WHERE source='pixiv' AND type IN ('novel','article') AND source_id LIKE 'novel:%' AND (preview_cover IS NULL OR CAST(preview_cover AS CHAR) IN ('null','{}')) AND id>? ORDER BY id LIMIT 200",&[json!(cursor.as_deref().unwrap_or(""))]).await?;
    let mut queued = 0;
    let mut last = None;
    for row in &rows {
        last = row["id"].as_str().map(str::to_owned);
        let history=tx.query(&format!("SELECT * FROM jobs WHERE kind IN ('import','import_content') AND {}='pixiv' AND {}=? AND (status IN ('queued','running') OR ({auto})) ORDER BY updated_at DESC,id DESC LIMIT 100",tasks::json_text(s,"payload","platform"),tasks::json_text(s,"payload","remote_id")),&[row["source_id"].clone()]).await?;
        if history
            .iter()
            .any(|j| matches!(j["status"].as_str(), Some("queued" | "running")))
        {
            continue;
        }
        let previous = history.iter().find(|j| {
            let p = json_column(&j["payload"]);
            p["auto_novel_cover"] == true && p["download"] == false
        });
        if let Some(previous) = previous {
            if tasks::date(&previous["updated_at"])
                .is_some_and(|t| t > chrono::Utc::now().naive_utc() - chrono::Duration::hours(24))
            {
                continue;
            }
            let mut p = json_column(&previous["payload"]);
            p["item_id"] = row["id"].clone();
            tx.execute("UPDATE jobs SET status='queued',progress=0,error=NULL,result=NULL,worker_id=NULL,lease_until=NULL,payload=?,updated_at=? WHERE id=?",&[json!(p.to_string()),json!(now()),previous["id"].clone()]).await?;
        } else {
            tasks::enqueue_import_tx(s,&mut tx,json!({"platform":"pixiv","remote_id":row["source_id"],"item_id":row["id"],"download":false,"auto_novel_cover":true})).await?;
        }
        queued += 1;
        if queued >= budget {
            break;
        }
    }
    tx.commit().await?;
    *cursor = if rows.len() == 200 || queued >= budget {
        last
    } else {
        None
    };
    Ok(queued)
}
