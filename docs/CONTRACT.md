# Implementation contract

Rust migration (2026-10-21): `server/rust/` implements all 152 legacy API routes,
platform connectors and eight native worker processes. AI inference remains an
independent service. `server/app/` is retained for compatibility fixtures and
rollback; existing production deployments have not been replaced. See
[`server/rust/README.md`](../server/rust/README.md) for configuration, validation
and migration boundaries. Rust defaults to port 8801, read-only mode and disabled
workers. It does not launch Python or forward business requests to the old API.

This repository implements a self-hosted library. Production: MySQL and mounted local/SMB/NFS storage. SQLite is an explicit local development/test mode, not the production database. Image inference remains local. Explicit metadata translation may use administrator-configured Ollama or an OpenAI-compatible endpoint; only titles and tags are sent, with user consent. No hosted publication.

## Ownership
- Backend agent: `server/app/{config,database,models,schemas,storage,library,auth,main}.py`, `server/tests/test_library.py`, `server/requirements.txt`.
- Integrations agent: `server/app/integrations/`, `server/app/ai/`, `server/app/jobs.py`, `server/app/integration_api.py`, `server/tests/test_integrations.py`. Do not edit backend core.
- Frontend agent: `web/` only.
- Flutter application: `app/`; independent Android/iOS UI and device session client.
- Root: deployment, documentation, integration fixes, QA.

Repository layout: web client in `web/`, Flutter client in `app/`, Python service in `server/`, deployment files in `deploy/`, documents in `docs/`, Git policy files in `config/git/`. The Python import package remains `app` under `server/app`; the Compose service name remains `app`. Container paths are `/app/server` and `/app/web/dist`. Deployment secrets live in `deploy/.env`; data, models, imports and database volumes remain at their original locations.

Run Compose through `python scripts/compose.py [--intel] ...`, `deploy/run.ps1 [-Intel] ...` or `sh deploy/run.sh [--intel] ...`. Each fixes `--project-directory` to the repository root, explicitly selects deploy/.env and deploy/compose.yaml, and preserves project name qiye-library and existing relative mount paths. The root build context selects deploy/Dockerfile and its Dockerfile-specific ignore file. Pytest configuration is server/pytest.ini; from the repository root use `python -m pytest -c server/pytest.ini server/tests -q`. Run host deployment checks separately with `python -m pytest scripts/tests -q`.

`scripts/configure_git.py` installs checkout-local absolute core.excludesFile/core.attributesFile paths and requires this project's own Git checkout. Bootstrap invokes it before generating credentials when a local .git exists; ZIP/NAS deployments without Git initialize normally and are told to configure ignores before later adding the project to Git. The deployment folder also carries a direct .gitignore safeguard for .env, with .env.example excluded from that rule.

## Multi-tag search and suggestions (2026-10-18)

`GET /api/items` accepts `tag_ids` (comma-separated stable tag IDs, at most 20 distinct IDs, each at most 36 characters) and `tag_mode=all|any` (default `all`). Empty selections apply no tag constraint. Unknown IDs match no tag; multiple assignment origins never duplicate items or affect pagination totals. Other filters remain conjunctive. The legacy `tag` parameter continues to accept one exact ID/name; if both parameters are supplied, its constraint is applied in addition to `tag_ids`.

`GET /api/tags?suggest=true&q=...&source=...` searches original names and Chinese translations, ranking case-insensitive exact matches before prefixes and substring matches. `%` and `_` in input remain literal characters. Suggestions default to 12 and are capped at 20, count only the selected IDs, and exclude unused tags. Ordinary paginated `/tags` remains compatible. No schema migration or external AI request is required.

Web and native App offer multi-select (at most 20), remove-chip controls, all/any mode, debounced suggestions and local search history. History retains at most 20 distinct keyword/tag/mode combinations; Web scopes by browser origin, App by bound server origin. Loading, invalid storage and late requests must not corrupt selections or overwrite newer suggestions. History never stores credentials and may be removed individually or cleared. Platform/content/favorite filters remain effective when combining tags; single-tag detail links continue to work.

## Shared backend conventions
FastAPI, SQLAlchemy 2, Python 3.12. `app.database.get_db` yields a SQLAlchemy Session; `app.database.SessionLocal` is a session factory. `app.config.settings` exposes `database_url`, `storage_root` (Path), `admin_password`, `secret_key`, `model_dir` (Path). Backend core exports `app.models.Base`, `Item`, `Asset`, `Tag`, `ItemTag`, `Job`, `SourceAccount`, `Subscription`, `ReadingProgress`; UUID IDs are strings. Integrations import lazily when useful.

Item fields: id,title,type,source,source_id,source_url,description,author,language,rating,created_at,updated_at. type is manga/illustration/novel/article; source is local/pixiv/ehentai/picacg/jm/kemono; rating is unknown/general/sensitive/adult. Assets: id,item_id,position,relative_path,original_name,mime_type,sha256,size_bytes,status. ItemTag has item_id,tag_id,origin (manual/source/ai),score,model_version. Tag: id,name. Job: id,kind,status,progress,payload(JSON),result(JSON),error,attempts,created_at,updated_at; kinds import/source_sync/ai; statuses queued/running/completed/failed/cancelled. SourceAccount: id,platform,label,credentials_encrypted(Text),created_at,updated_at. Subscription: id,platform,target_type,target,label,enabled,auto_download,created_at,updated_at,last_checked_at,last_error. ReadingProgress: item_id,page,updated_at. Backend may add necessary fields.

## HTTP contract
All APIs at `/api`. Login required except health, auth status/login and one-time device pairing exchange. Administrator login uses an HttpOnly SameSite cookie; mobile sessions use scoped Bearer credentials. Password from LIBRARY_ADMIN_PASSWORD; local development default is `local-library`, production compose requires an explicit password. Web UI obtains status before rendering library. Errors are `{detail: string}` or sanitized validation errors without raw request inputs.

Native mobile administration uses a separate in-memory administrator cookie obtained through the existing password login. It does not promote, replace or persist the QR reading credential. `GET /mobile-admin/catalog` requires administrator-cookie authorization and describes native forms for the existing management routes, including typed nested fields and file transfers. It executes no actions itself; all original validation, authorization, queue and upload boundaries still apply. Close/logout drops the mobile admin session; expiry returns to admin login while keeping reading bound. Management downloads are bounded to 256 MiB on the phone. File selection and saving use native OS dialogs.

- GET /health -> {status, database, storage}
- GET /auth/status -> {authenticated: bool}
- POST /auth/login {password} -> {ok:true}; POST /auth/logout
- GET /dashboard -> {total_items,total_pages,total_bytes,pending_jobs,untagged_items,recent_items:[ItemView],source_counts:{},rating_counts:{}}
- GET /items?q=&type=&source=&tag=&rating=&page=1&page_size=30&sort=latest -> {items:[ItemView],total,page,page_size}
- GET /library/collections -> [{source,name,storage_directory,item_count,saved_item_count,metadata_item_count,page_count,size_bytes,legacy_asset_count}]. Physical namespaces: collections/{source}/blobs, dedupe within source only; manual uploads are local.
- GET /items/{id} -> ItemView with assets and progress
- PATCH /items/{id} {title?,description?,author?,language?,rating?,tags?:string[]} -> ItemView (manual tags survive AI runs)
- DELETE /items/{id} -> {ok:true} (library removal only; keep blobs)
- POST /imports/upload multipart `files` repeated, `title` optional, `type` default manga -> ItemView. Accept images, ZIP/CBZ, TXT/MD. Enforce extraction path and size limits. Text stored as a text/plain asset, never untrusted HTML.
- POST /imports/folder {path,title?,type?} -> ItemView. Restrict to configured LIBRARY_IMPORT_ROOT, not arbitrary filesystem.
- GET /assets/{id}/content -> authenticated image/text file
- GET /items/{id}/export -> authenticated ZIP/CBZ download
- GET /assets/{id}/text-sections -> {sections:[{index,title,text}]}; authenticated UTF-8 plain-text navigation, explicit Pixiv chapter/newpage markers only; original asset remains unchanged.
- PUT /items/{id}/progress {page:int,section?:int=0,offset?:float=0} -> {page,section,offset,updated_at}; zero-based asset and text-section indexes, offset in [0,1]. Text section bounds are checked against the stored original.
- GET /tags?q=&source=&limit=&offset=0 -> [{id,name,name_zh,display_name,count}]; q matches original and Chinese names, count is distinct works. Existing unpaginated requests remain supported.
- GET /jobs -> {items:[JobView]}
- GET /jobs/{id} -> JobView, including jobs outside the latest-200 list.
- POST /jobs/{id}/retry -> JobView; POST /jobs/{id}/cancel -> JobView
- GET/PUT /queue/settings -> {paused,rate_limit_enabled,min_interval_seconds,max_interval_seconds,concurrency}; PUT accepts partial settings, validates 0 <= min <= max <= 60, a strict boolean rate_limit_enabled and a strict integer concurrency in 1..8 (default 1). Concurrency changes persist without restart; lowering the limit drains existing tasks without cancellation, and toggles never resume a paused queue. Legacy request_interval_seconds sets equal bounds and cannot be combined with min/max. rate_limit_enabled defaults true; false skips local random pacing and clears its pending reservation without changing pause, intervals or concurrency. Upstream 429/Retry-After cooldown remains enforced independently. QueueControl persists concurrency (migration f36ac740b51d), rate_limit_enabled, next_request_at (local reservation) and backoff_until (upstream cooldown). Migration e259b62f943a conservatively copies an existing legacy next_request_at into backoff_until once, because old versions combined both waits. Existing credentials and authentication rate limits are unaffected.
- GET /queue/status -> {settings,counts:{queued,running,failed},active:JobView|null,active_jobs:JobView[]}; counts and active_jobs include only acquisition jobs, ordered by created_at/id, with no payload/credentials. Legacy active is the first running job or null.
- POST /queue/covers {source?:pixiv|ehentai|picacg|jm|kemono} -> {queued,skipped,has_more,message}; queue at most 500 missing previews, idempotent, no body download or subscription changes.
- POST /items/{id}/download -> JobView; idempotently request a remote item's body, using the preview stage when necessary.
- GET /sources -> [{platform,label,status,capabilities:[{key,label,status}],accounts:[{id,label}],message}]
- POST /sources/{platform}/accounts {label,credentials:{...}} -> {id,label,platform}; never return credentials
- POST /sources/{platform}/import {url?:string,remote_id?:string,account_id?:string} -> JobView; idempotently queue the preview stage, then queue body acquisition after metadata/cover commit.
- POST /sources/{platform}/sync {account_id?:string} -> JobView
- GET /subscriptions -> {items:[SubscriptionView]}; POST /subscriptions {platform,target_type,target,label,enabled,auto_download}; DELETE /subscriptions/{id}
- Pixiv SubscriptionView/create/update add content_scope: images|novels|all. Existing subscriptions migrate to images; enabled and auto_download preferences stay unchanged. images checks both illustration and manga author lists; novels uses a separate novel:ID namespace. Other platforms retain images.
- POST /series/import {platform:pixiv,url_or_id,kind?:novel|manga,account_id?} -> JobView; deduplicated series_sync job. A numeric series ID requires kind; recognized official series URLs determine kind.
- GET /series/{id} -> {id,title,origin:source|manual,source,source_id,kind,complete,revision,entries:[{item_id,title,position,label,content_state,page_count}]}. Entries are ordered, and every member has a local Item even before its body is downloaded.
- POST /series {title,kind,entries:[{item_id,label?}]} -> SeriesDetail. Manual cross-work grouping, at least one existing item, no duplicate members.
- PUT /series/{id} {revision,title,entries} -> SeriesDetail; manual series only, stale revisions return 409, empty entries unlink all members without deleting works. Official series order is updated only by the provider import.
- GET /ai/status -> {available,models:[{id,label,available,message}],message}
- POST /ai/analyze {item_ids:string[]} -> {jobs:[JobView]}
- POST /ai/characters/search {query,mode:character|work,limit=6} -> {provider,query,mode,candidates:[{id,name,native_name,aliases,works:[{title,type,source_url}],description,source_url,provider}],notice}. Explicit text only, fixed AniList API, library proxy applied. Work mode searches manga.
- POST /ai/characters/confirm {item_id,name} -> {item_id,tag:{id,name,origin:manual,score:null}}. Atomically append one role tag without replacing existing labels; idempotent.
- GET/PUT /ai/characters/image/settings: public {provider,has_api_key,max_file_bytes,notice}; PUT {api_key?} keeps omitted/null secret, empty clears, encrypted storage.
- POST /ai/characters/image-search multipart {file OR asset_id,consent:true} -> {provider,matches:[{similarity,index_name,title,creator,characters,material,source_urls}],uploaded_size,notice}. Exactly one explicitly approved image; fixed SauceNAO endpoint, bounded re-encoded JPEG, no thumbnails/automatic tagging. Upstream failure is distinct from empty matches.
- GET /settings -> {storage_root,import_root,database_kind,storage_available,ai_mode}; no secrets
- POST /database/export -> authenticated `application/sql; charset=utf-8` attachment `qiye-database-YYYYMMDD-HHMMSS.sql` with `Cache-Control: no-store`. MySQL/PyMySQL only (501 otherwise), 409 for concurrent export, sanitized 503 for failure. Exports application table definitions/data and Alembic version from an InnoDB consistent snapshot; no media, environment keys, DROP/USE/CREATE DATABASE. Restore into an empty MySQL database. Temporary file and single-export lock are released after response completion/disconnect; generation is bounded to 1 GiB/600 seconds with bounded database I/O.

### Kemono and platform API endpoints

Kemono uses the existing `/sources/kemono/import` workflow with an official post URL or `service:user:post` remote ID, anonymous public access, and an independent `collections/kemono` namespace. Author subscriptions accept official creator URLs or `service:user`; tag and search subscriptions are supported, with a minimum of 3 characters for search and a maximum of 200 for tag/search. Public access does not expose a login or account-follow synchronization action in Web.

- GET /platform-endpoints -> {items:[{platform,label,default_url,custom_url:string|null,enabled:bool,effective_url,description}]}
- PUT /platform-endpoints/{platform} {custom_url:string|null,enabled:bool} -> the updated item. Web uses its existing administrator cookie. Each write updates one platform only; no revision field is used.

Defaults: Pixiv `https://app-api.pixiv.net`, PicACG `https://picaapi.picacomic.com`, E-Hentai `https://api.e-hentai.org/api.php`, JM `https://18comic.vip`, Kemono `https://kemono.cr/api/v1`. Custom endpoints require public HTTPS and a compatible platform protocol. Requests stay server-side, and credentials required by that platform may be sent to its configured endpoint. Pixiv OAuth remains official. The library's own server URL and client binding do not change. Disabling an endpoint retains its custom URL; Web's restore-default button clears the draft and requires saving to persist.

Video files use existing Item types and Assets (`video/mp4`, `video/webm`, `video/quicktime`); mixed posts use article. The default file limit is 128 MiB and Kemono's per-work total limit is 512 MiB. Unsupported archives/embedded media fail body acquisition without claiming a complete download. Web uses a native video element with controls, inline playback and metadata preloading; the authenticated asset URL also supports original-file download. ReadingProgress.page identifies the asset, section is 0, and offset is a 0..1 playback fraction. Initial metadata/seek restoration and media errors do not overwrite prior progress with 0. Mixed-file navigation crosses text section boundaries and remembers each file's position during the open reader session. Supported video encoding depends on the browser.

### Platform login and proxy settings
- Source account responses also include `auth_status: verified|unverified`, `verified_at: ISO UTC|null`; advanced `/accounts` never claims verified. Successful login stores only encrypted issued sessions, never submitted passwords.
- POST /sources/{platform}/login {label,username,password} -> account (PicACG/JM/E-Hentai).
- POST /sources/pixiv/oauth/start -> {session_id,authorization_url,expires_in}; POST /sources/pixiv/oauth/finish {session_id,code,label} -> account. Explicit manual callback/code entry, same library session, one use. External auth failures use 400/502, not 401 (which means local session expired).
- GET /network/proxy -> {enabled,proxy_url,proxy_username,has_proxy_password,controller_url,has_controller_secret}; PUT accepts {enabled,proxy_url,proxy_username,proxy_password?,controller_url,controller_secret?}. Omitted/null secrets retain existing value, empty string clears; URL fields never embed credentials.
- GET /network/clash/groups -> {groups:[{name,type,now,nodes:[{name,type,alive,delay}]}]}; lists manually selectable groups and existing delay samples.
- POST /network/clash/select {group,node} -> {ok,group,node}; validates current group membership before controller mutation.
- POST /network/proxy/test {platform} -> {ok,reachable,status_code,elapsed_ms,message,via_proxy}; fixed public target, no account credentials sent. HTTP rejection is distinguished from network failure.

`app.network.httpx_proxy()` and `requests_proxies()` are the shared outbound configuration. Controller requests do not use the content proxy. Settings persist encrypted in `NetworkSetting`; no cloud proxy service is provisioned.

### Embedded subscription proxy
- GET /network/managed -> {enabled,core:{installed,running,version,state,error,node_count,selected,proxy_url,restart_count},subscriptions:[{id,label,url_host,node_count,auto_update,interval_hours,fetch_via_proxy,last_updated_at,last_error,next_update_at}],nodes:[{id,name,type,subscription_id,subscription_label,selected,delay}],selected_node_id,message}. Never return subscription URLs or node credentials.
- POST /network/managed/subscriptions {label,url,auto_update=true,interval_hours=24,fetch_via_proxy=false}; PATCH /network/managed/subscriptions/{id} accepts optional same fields; POST .../{id}/refresh; DELETE .../{id}. All return public state. Intervals 1–168h, HTTPS public subscription URLs only.
- POST /network/managed/enable {enabled}; POST /network/managed/select {node_id}; POST /network/managed/restart return public state.
- POST /network/managed/delay {node_id} -> {node_id,delay,ok,message}.
- POST /network/managed/delay-batch {node_ids?:string[]} -> 202 {batch_id,status,total,completed,succeeded,failed,started_at,finished_at,message,results:[{node_id,status,delay,ok,message}]}; omitted IDs tests all nodes with six bounded workers. Repeated concurrent starts reuse the running batch. GET the same path returns the latest batch or null; GET /network/managed/delay-batch/{batch_id} returns a batch or 404 when expired/restarted. Per-node status: pending/running/success/failed/skipped. Neither single nor batch diagnostics change selection or enablement; stale results from changed/deleted node configurations are skipped. This measures fixed-target connection latency, not download throughput.
- Measurement rows include `checked_at` (UTC, microsecond precision; null before probing), and public nodes include `delay_checked_at`. Older results cannot overwrite newer measurements in storage or the UI. `skipped` does not clear a saved delay. Batch messages surface persistence failures even when probing has completed.

Embedded Mihomo has priority when enabled. A missing selected node routes to REJECT, even if enabled, and a dead core never triggers direct fallback. Imports validate with the bundled fixed-version core before encrypted DB commit. Lifecycle owns one nonprivileged loopback-only child process and one subscription updater; no multi-worker support. Tests disable runtime lifecycle and strictly isolate all database and storage access.

ItemView: id,title,type,source,source_url,description,author,language,rating,tags:[{id,name,origin,score}],page_count,size_bytes,cover_url,created_at,updated_at,progress:{page}|null; detail adds assets:[{id,position,original_name,mime_type,size_bytes,url}]. cover_url and asset url are same-origin `/api/...` URLs. Non-image items can have null cover_url. JobView: id,kind,status,progress,error,created_at,updated_at,result. Frontend gracefully handles unavailable integrations but must never fake successful imports or AI output.

## Application integration

### Mobile device pairing

- POST /devices/pairing `{server_url}` requires an administrator browser cookie and returns `{id,server_url,expires_at,qr_payload}`. QR payload is a JSON string `{type:'qiye-library',version:1,server_url,pairing_token}`; expires after five minutes. Creating a new QR atomically replaces the previous pending code. Generating it grants binding authorization.
- POST /devices/pairing/exchange `{pairing_token,device_name}` is unauthenticated and rate limited; atomically consumes the code and returns `{device_id,device_name,access_token,expires_at,server_name:'栖页'}`. Fixed device lifetime is 90 days. Only SHA-256 token digests are stored, never administrator passwords or raw bearer tokens.
- GET /devices requires an administrator cookie and returns `{items:[{id,device_name,created_at,last_used_at,expires_at,revoked_at,status}]}`; status is active/expired/revoked. DELETE /devices/{id} revokes a device and returns `{ok:true}`. POST /devices/disconnect accepts a device Bearer token and revokes itself.
- Device permissions are an exact method/path allowlist: GET health/dashboard/collections/items/tags, item detail/cover/export, asset content/thumbnail/text-sections and series detail; PUT item progress; POST item download and self-disconnect. All management APIs return 403 for a valid device token, even if an administrator cookie is also present. Tokens are accepted only in Authorization headers; expired/revoked tokens return 401. Existing cookie and write-origin protections remain.
- Devices additionally allow GET `/translation/status` and `/items/{id}/translation`, and POST `/items/{id}/translate` with `{consent:true,force?:false}`. Model configuration, test and batch endpoints remain administrator-only. No endpoint URLs or model credentials are exposed to devices.
- Server roots must be HTTP(S), without userinfo, query, fragment or non-root path; HTTP is permitted for local addresses and public hosts require HTTPS. No administrator password is encoded in the QR.

### Metadata translation and navigation

- Item views add nullable `title_zh` and `display_title`; a translation is valid only for its original title snapshot. Editing a title invalidates its Chinese translation. Tags add nullable `name_zh` and `display_name`; original names and tag identities remain unchanged. `/items` accepts `author` alongside `source` for exact author-name filtering, and `q` searches original and translated names.
- GET/PUT `/translation/settings` is administrator-only. Public settings: enabled, provider (`ollama|openai`), base_url, model, use_proxy, has_api_key. PUT omits/null api_key to retain it only for an unchanged endpoint, empty clears it; changing provider or base URL clears a previous key. Secrets are encrypted and never returned. GET `/translation/status` exposes only enabled/configured/provider/model/notice.
- POST `/items/{id}/translate` `{consent:true,force?:false}` returns `{status,job_id,cached}` and deduplicates active `translate` jobs. GET `/items/{id}/translation` returns only that work's status/progress/error/job_id. Existing tag translations are reused; originals are preserved, malformed results are rejected atomically, and deleted or edited works cannot be resurrected by late results.
- POST `/translation/test` translates a fixed synthetic title and tag. POST `/translation/batch` `{consent:true,limit?:50}` queues at most 50 missing works. Both require administrator access. Translation is disabled until configured, sends only title/tag text, and never installs models automatically. No translation provider credentials are included in job payloads.

### Pixiv author profiles and bookmarks

- Items add nullable `author_source_id`; Pixiv RemoteWork adds optional `author_id`. Detail may resolve a missing ID from one unambiguous followed-author record. Existing author names and edited metadata are not replaced. Three new tables store author profiles, per-account feeds and ordered feed membership; profile avatars use the Pixiv blob namespace.
- GET `/pixiv/authors?q=&page=1` lists cached and followed authors, 30 per page. POST `/pixiv/authors/open` `{url_or_id,account_id?}` queues `author_profile` and returns normalized remote_id/section/category/restrict plus a safe job summary. `me` resolves the authenticated Pixiv owner. GET `/pixiv/tasks/{id}` exposes status/progress/error and author/feed IDs for author jobs only, never job payloads or credentials.
- GET `/pixiv/authors/{remote_id}` returns cached name, description, same-origin avatar URL, statistics, synced_at and can_read_private; unknown profiles return an explicitly unsynced placeholder. GET `/pixiv/authors/{id}/avatar` serves the stored image.
- GET `/pixiv/authors/{id}/feed?section=works|bookmarks&category=illustration|manga|novel&restrict=public|private&account_id=&page=1` returns Item views (24/page), total, revision, complete, unavailable, has_more and a safe job summary. POST the same path with `{section,category,restrict,account_id?,more?:false}` queues `author_feed`; no cursor/URL is accepted from clients. Active jobs are deduplicated by account and feed scope. Private collection access requires a verified owner, and is checked again against authenticated Pixiv identity before remote requests.
- Both new kinds are acquisition jobs: existing pause, pacing, lease, cancellation and retry rules apply. Cursor paths, owner, category, privacy and keys are validated. Image bookmarks are filtered by actual type, up to three raw pages per request; empty filtered pages retain continuation. Profiles/covers are separate from bodies. Refresh generations preserve old tails until a complete scan; directory removal never deletes works. Late results cannot resurrect known members deleted during fetch. Device permissions include only the exact author GET/POST routes above.

### Pixiv novels, multiple pages and cross-work series

One remote work remains one Item. Novel source IDs are `novel:ID`, distinct from numeric illustration/manga IDs. Item views add `remote_page_count` and nullable `source_series:{remote_id,title,kind}`; `page_count` always counts locally stored assets. Detail adds `series` summaries with the same fields as SeriesDetail except entries. Reading progress adds section and offset.

`RemoteWork` includes remote_page_count and source_series. Pixiv multi-page bodies must match the declared page count; links are validated before body acquisition and retain the provider's order. Novels retain original TXT; reading views parse explicit chapter/newpage delimiters without executing HTML or fetching embedded references. Text parsing is bounded to 16 MiB and 5000 sections.

`PixivConnector.fetch_series(remote_id,kind,checkpoint)` returns `{remote_id,title,kind,complete,works:[RemoteWork]}` in provider order, metadata only. Pagination permits only fixed Pixiv paths, matching series IDs and supported cursors; bounded to 1000 works/50 pages. Explicitly invisible, structurally valid placeholders are skipped and force complete=false; malformed visible entries still fail. The worker creates/merges metadata, updates official Series/SeriesEntry relations, and queues missing previews with download=false. No whole-series body acquisition is implied. A partial refresh preserves an existing directory instead of truncating it. Manual and official series coexist; titles never determine membership.

### Staged acquisition

`import` saves basic metadata and an optional `RemoteWork.preview_cover` tuple `(name,bytes)`; `import_content` downloads full content. The preview is resized/re-encoded and stored as a separate `Item.preview_cover` JSON blob reference in the platform collection, never as an `Asset`. It therefore does not affect page count, reader progress, exports or AI page analysis. `GET /items/{id}/cover` serves the local preview with normal authentication. A failed optional preview must not prevent metadata creation or requested body queueing.

Item responses add `content_state`, `content_error`, `content_job_id`. Existing body assets migrate to `ready`; metadata-only records remain `metadata`. Successful body import preserves user metadata, manual tags and reading progress. Subscription `auto_download=false` still means no automatic body download; preview backfill does not change this preference.

The persistent worker uses nine threads, with a database-serialized claim transaction enforcing the configured 1..8 acquisition slots and one separate shared slot for local AI/translation. Counts include valid leases across processes; expired leases are recovered during claims. Each attempt has a unique owner token so an expired attempt cannot checkpoint, commit, complete or fail a newer attempt in the same process. Acquisition prioritizes previews while allowing the oldest non-preview job after four preview claims. `app.queue_control.wait_for_request()` gates actual platform HTTP attempts, SDK retries and supported redirects only inside a thread-local acquisition context. Default request spacing is uniform random 2–5 seconds; min/max are validated within 0–60 seconds and equal values give a fixed interval. All concurrent acquisition tasks share the same persistent request gate and upstream cooldown; concurrency is per task, preserving page order within a work. Pacing does not apply to local AI, account login or proxy diagnostics. Shutdown cooperatively interrupts all lanes with a total bounded two-second join.

`QueuePaused` derives from `JobCancelled`: the current job returns to the queue, retaining committed metadata/cover. Wait loops release database transactions and renew/check ownership while waiting. Body data is committed atomically after a complete fetch; this is job retry, not a page-level resumable downloader.

Core main app includes router from `app.integration_api` if present. Integration router exposes all source/job/subscription/AI routes; core owns library/auth/settings routes. Export integration lifecycle `start_worker()` and `stop_worker()` (async or sync clearly documented) for lifespan integration. Worker uses real persisted jobs and recovers interrupted work. Integrations should expose an import helper API request if needed or call `app.library.import_files` after agreeing exact signature with core owner. All workers run local inference only; missing model means actionable failure, not synthetic classification.

## Frontend design
Chinese product name `栖页 · 私人图书馆`. Cool white canvas, graphite sidebar and indigo accents; semantic success/warning/error colors retained. Body text 15px, secondary text at least 13px, responsive mobile reading. No seeded pretend library data. Screens: library/dashboard, source connectors, subscriptions, tags, tasks, AI review/status with explicit character lookup, settings, reader. Vite proxy `/api` to http://127.0.0.1:8000. Build static dist served by FastAPI/Docker. Lucide icons may be used. No external content thumbnails.

AI status additionally exposes requested_provider, actual_provider, device, fallback_reason and execution_state. Actual fields remain null until inference succeeds. auto/intel recover to CPU on accelerator failure; explicit cuda stays strict. Intel runtime uses an isolated child and never accesses network or DB in that child.



## Android 在线更新（0.4.0）

`GET /api/app-updates/android` 需管理员会话或有效设备 Bearer，返回 `{release: null | {package_name, version_code, version_name, sha256, certificate_sha256, size_bytes, min_sdk, notes, published_at, download_path}}`。最高 versionCode 是当前发布版本；无外部下载地址，无客户端提交的包信息。

`POST /api/app-updates/android?notes=...` 仅管理员 cookie 可用，原始 APK body，Content-Type `application/vnd.android.package-archive`，最大 256 MiB，说明最多 2000 字。按流计数，上传超时 300 秒，校验工具每次最多 90 秒；失败清理临时文件，不发布。apksigner 完整签名验证（含 v2）和 aapt 元数据解析必须通过，包名固定 `app.qiye.qiye_library`，拒绝 debug 配置。发布证书默认沿用现有 release 证书，定制构建可通过服务端环境变量 `LIBRARY_ANDROID_CERT_SHA256` 指定。依赖缺失时返回 503，禁止跳过校验。Docker 内置 Debian apksigner、aapt 和 Java，电脑直接运行服务需自行安装或设置 `LIBRARY_APKSIGNER`、`LIBRARY_AAPT` 工具路径。

`android_releases` 以 version_code 为主键、sha256 唯一，旧版不可替换或降级，同版本同内容重试幂等。并发冲突返回 409；并发发布的最终当前版本始终取最高 versionCode。原文件用 collections/local/blobs 哈希存储，先持久化文件再提交记录，备份脚本已有覆盖；SQL 导出包含发布记录。

`GET /api/app-updates/android/{64位小写sha256}/apk` 需管理员或设备授权，只能下载已发布文件；服务端读取前校验实际大小及哈希。设备不能发布安装包。客户端不跟随重定向，不将凭据放进 URL，流式写入私有缓存并验证哈希，原生桥再校验包名、更高版本和与当前安装一致的签名。只通过限定缓存子目录的非导出 FileProvider 向系统安装器临时授予读权限，Android 未允许安装未知来源时先打开系统授权页，用户返回后点击继续安装。无静默安装和任意路径共享。iOS 不提供 APK 安装。

App 列表累积已成功的分页，加载中去重、失败不推进页码，刷新及搜索从第一页开始，并用请求序号隔离迟到响应。作者目录响应增加 generation，远端刷新代次变化时丢弃旧分页重新读取第一页。滚动到远端缓存末尾只排队读取下一批资料，不自动下载正文。


## 已下载分类与 App 导航（0.4.1）

`GET /api/items?downloaded=true` 在数据库查询阶段筛选有正文 Asset、所有 Asset 状态均 available 的作品；图像作品另要求本地页数不小于已知远端页数。封面 preview_cover 不计作正文，无 Asset 的 ready 标志也不算已下载。小说的章节数不作为图像缺页判断。筛选在统计及分页之前执行，与 q/source/type/tag/author 条件组合，设备沿用原有 items 只读授权。

文件状态由存储巡检／启动 reconciliation 维护；文件实际读取时仍校验磁盘可用性，并不在每次列表查询扫描整个 NAS。完整分类表示服务器已存正文，手机离线整书缓存另行实现。

App 主导航使用原生 Drawer，顶部保留搜索。已下载模式在搜索和追加分页时保留；切换具体平台可继续组合，点击全部书库清除已下载模式。列表移除固定数量／末页提示，加载与重试控件放入滚动内容末尾，阅读器进度控件保留。


## Pixiv 主副账号分工

管理员 cookie 专用 `GET/PUT /api/sources/pixiv/account-roles`。PUT 参数 `{enabled, main_account_id, content_account_ids:string[]}`，副账号数组不设数量配额；兼容旧 `content_account_id` 参数，读取旧加密配置时规范化为单元素数组。复数参数优先，兼容输出的单数始终等于首项或 null。GET 还返回 main、content、contents 的 `{id,label,status,verified_at}` 状态（verified／unverified／missing／unconfigured），以及 download_accounts（全部已保存的 Pixiv 账号和失效配置引用），附 in_pool/running/completed/failed 统计。统计只按分配给该账号的当前任务状态汇总，不暴露凭据或任务 payload。设备 Bearer 不能读写账号配置，带管理员 cookie 也不能提升 Bearer 权限；既有 Origin 校验继续执行。

配置以 NetworkSetting 的 pixiv_account_roles 键加密保存，沿用配置备份。启用需要一个主账号并为至少一种功能分配账号；主账号不能承担图片下载，可显式兼任作品详情／小说正文和作者资料。各功能池内拒绝重复 ID、同 refresh_token 以及已知相同 user_id，同一账号可跨功能勾选；验证失败不覆盖已有分工。关闭时可清除已删除的账号引用以恢复旧选择。QueueControl.download_account_cursor 在迁移 f36ac740b51d 添加，参与领取事务锁；任务 payload.download_account_id 保存选中的副账号。选择最少运行任务的可用副账号，同负载依次轮换，自动暂停／重启／租约恢复保持分配，明确 POST retry 清除旧分配。移除的账号不接受新任务；已绑定任务不自动换号，重试可重新分配。配置损坏仍允许领取后明确失败，避免全队列无限等待。上游失败不会触发自动换号或主账号回退。

jobs.account_credentials 按请求用途路由：metadata 使用主账号检索作品、同步关注及作者作品／收藏／系列目录；checks 使用作者资料账号读取指定作者主页／账号资料，当前账号 me 的身份资料仍使用主账号；details 使用作品内容账号读取作品详情、预览封面及小说正文；content 使用图片下载账号获取原图。组合 import 和旧 import_content 也按内容类型选择功能池，并验证已分配账号仍在配置中。其他平台保留原行为。长请求使用开始时解析的凭据，失败不追加主账号尝试。新建关注订阅记录实际主账号。分工开启时图片下载阶段不覆盖资料阶段的原来源标签或分级，原有标题／作者保留逻辑继续适用。

作者范围 API 以主账号计算账户 scope，既有作者任务必须与当前主账号绑定一致才执行，否则提示重新获取；绝不把原副账号／旧主账号的私密目录重标为新身份。所有远端访问仍由 Pixiv 自身权限约束。图片下载阶段会以下载账号读取取得原图地址及校验权限所需的作品详情／原图目录，未承诺下载账号仅发送原图字节请求；小说正文由作品内容账号获取。


## Pixiv 合并下载任务

Pixiv 的 import 且 download=true（含省略）的任务在一个租约内完成详情和完整正文，成功后一起提交，不再派生 import_content。已有作者／系列目录、订阅只建档及补封面路径保持 download=false。其他平台仍走原两阶段流程。旧已排队的 Pixiv import_content 继续兼容执行，不批量取消用户任务。

单账号直接调用 fetch(download=true)，只读取一次作品详情，再取所有原图或小说正文。启用账号分工时，图片作品由作品内容账号 fetch(download=false, include_preview=false) 读取资料，再由图片下载账号按自身权限 fetch(download=true) 获取原图；小说由作品内容账号同时读取详情与正文。下载账号仍需发起取得原图地址及校验权限所需的请求。未配置对应功能或账号失败时不回退到主账号。

完整性及来源 ID 校验后合并资料、原文件、标签和分级，在同一数据库事务提交。用户人工标题、标签、分级保持；缺页、无正文或取消不发布新条目；已有预览与资料不丢弃。导入事务内不通过独立会话写心跳，以免 SQLite 交叉会话锁等待，提交时仍校验任务租约与暂停／取消状态。原文件先校验写入，未提交引用的文件仍是可回收的孤立 blob，备份一致性规则不变。

Job 继续使用 kind=import，安全输出新增 label='Pixiv 详情与正文下载'，供任务页和采集队列展示；历史已完成的详情／封面记录保留原标签。结果 content_job_id 为当前任务 ID，没有额外正文队列项。图像封面由完整正文缩略图提供；浏览目录时已有的预览仍可用。


## 人物作品墙（0.5.0）

认证接口 GET /api/characters?q=&source=&downloaded=&page=&page_size=&sort=count|name，聚合角色标签的不同作品数，支持角色:／人物:／character: 前缀，中英文冒号。搜索本地标签、译名、缓存的人名和别名，分页前应用筛选。GET /api/characters/{tag_id} 返回人物资料和关联作品计数／封面。

POST /api/characters/search 接受既有 character/work 搜索参数；POST /api/characters {candidate_id:int} 新增人物，POST /api/characters/{tag_id}/profile {candidate_id:int} 为现有标签绑定资料。服务端按正整数候选 ID 重新获取 AniList 资料，规范来源链接后保存；失败保留原资料，不接受客户端任意介绍或 URL，不自动为作品添加标签。character_profiles 以 tag_id 为主键，保存介绍、别名、原作、来源及检索文本；包含数据库导出和备份。设备 Bearer 可使用这些指定路径，不能访问管理员配置。

## 独立队列及账号功能（b58ce962d713）

QueueControl.lanes JSON 持久化六类队列的 paused/concurrency：download、details、checks、ai、translation、other。NULL 旧配置按三个网络队列继承旧 concurrency，其他默认 1，均默认未单独暂停。Pixiv import 且 download 非 false 归下载；其他 import 归详情；import_content 归下载；source_sync/series_sync/author_profile/author_feed 归检查；ai 和 translate 各自独立。后台执行器上限 48，数据库领取锁同时校验每个队列与账号的额度，跨进程共享。

GET /api/queue/status 保留旧字段，新增 lanes[{id,label,paused,concurrency,blocked_by_master,counts}]。PUT /api/queue/lanes/{id} 接受严格布尔 paused、严格整数 concurrency(1–8) 的部分更新。GET /api/jobs?queue={id} 在取最近 200 条前筛选队列；Job 安全输出增加 queue，仍不返回 payload。旧 PUT /api/queue/settings concurrency 兼容为统一设置三个网络队列的上限，不代表三队列合计上限；paused 仍为网络采集总开关。本地 AI／汉化由自身开关控制，提交前也检查暂停。

Pixiv roles PUT 增加 detail_account_ids、check_account_ids 与 account_limits:{account_id:1..8}。账号可多选图片下载、作品详情／小说正文、作者资料功能，各功能空池均不自动回退主账号；需要主账号兼任详情或作者资料时必须显式勾选。account_limits 默认 2，覆盖该账号所有并行任务；一个任务同一账号承担多个功能只占一格，多账号各占一格，领取时保守占用至任务结束。未开启分工时原选中账号也受账号并发限制。旧客户端省略新增字段时保留已有配置；损坏配置可用合法配置替换。

任务 payload 分别持久化 download_account_id/details_account_id/check_account_id，未开启分工时保存 execution_account_id。暂停／重启不重分配；明确 retry 清除这些字段。排除已饱和身份后再取候选，避免大量等待任务阻塞其他平台。账号统计按每任务／账号去重，并包含所有功能的运行／完成／失败状态。凭据路由 purpose=details 用于作品详情与小说正文，checks 用于指定作者主页资料，metadata 用于主账号检索和目录同步（含公开作者作品、收藏、系列、订阅及关注）；me 的自身资料也固定主账号。旧主账号目录 scope 校验保留。

Pixiv 作者订阅新增时未显式指定 content_scope 则使用 all；其他订阅默认 images，显式选择仍尊重。关注同步产生的作者同样使用 all。本次维护将现有 594 条 Pixiv 作者订阅启用并改为 all，不改变 auto_download、账号、周期与目标。


## 工作进程与 CPU 亲和性

默认 LIBRARY_WORKER_MODE=process，Web 监督六个 app.worker 子进程，每进程仅领取对应 lane。领取与执行分离，执行仍通过原租约 ContextVar，队列／账号额度在同一数据库领取锁内检查。每进程一个调度器，最多 8 个按需线程，空闲退避 2–5 秒，有活动任务时每秒检查。preflight 不创建空闲写锁，并在获得领取锁前结束读事务以获取最新 MySQL 快照。按 kind 的队列条件可利用现有领取索引，订阅查重改为批量读取。

CPU 配置为 auto/off/完整 JSON，逻辑 CPU 不足时共享。GET /api/queue/status.runtime 返回 mode、api_pid、api_cpus、affinity_applied、shared_cpus 和 workers[{lane,pid,state,cpus,requested_cpus,affinity_applied,active,restarts}]；state 为 starting/running/restarting/unresponsive。实际 cpus 由子进程读取，父进程校验 PID、心跳时效并过滤内部字段。Windows venv 启动直接使用基础解释器并保留 venv 前缀，避免重定向启动器的 PID 与实际工作进程不一致。

每个 MySQL 工作进程连接池 pool_size=2/max_overflow=8/pool_timeout=10；API 连接池独立。进程退出时只使它报告的确切租约令牌过期，旧尝试提交继续受原 fencing 保护。Linux 进程组结束会清理推理后代，父进程丢失也退出；自动重启退避 1–60 秒。Web 状态查询锁不跨 DB 恢复请求。AI 状态通过本地有界状态文件返回，模型时间戳／provider 变化会使缓存失效；不把状态文件纳入数据库恢复要求。主进程单独管理内置代理，工作进程只使用相同回环端口。

本次没有新增数据表或结构迁移，版本仍为 b58ce962d713。LIBRARY_WORKER_MODE=thread 保留旧线程模式供兼容调试。更多部署说明见 WORKERS.md。


## Pixiv 作品不可见及限流诊断

_metadata_work 遇到显式 visible=false 时先报告当前账号无法访问，之后才执行标题、ID、页数和正文完整性校验；系列目录已有的隐藏成员跳过逻辑保持。真正缺失必要字段仍作为格式不完整，不从占位数据创建作品。SDK 传输层可由 Pixiv 指定 HTTP 429 固定错误，先保留 Retry-After 等待，再跨过 SDK 的宽泛异常处理边界返回 ConnectorError；_api 认证阶段保留该错误，不重写为登录失败。其他 SDK 默认行为保持。

Pixiv 详情／正文 fetch 的安全 ConnectorError 增加实际账号名称与执行阶段，上下文在开始网络请求前解析，不在网络期间持有额外数据库事务。取消和租约中断不被该错误包装捕获，账号权限失败不自动回退。旧失败记录不批量改写为未经验证的新原因，也不自动重试。

## 账号列表与 App 返回状态

Pixiv account-roles 的 download_accounts 仅列出仍存在的 Pixiv SourceAccount。缺失的分工引用通过 missing_account_ids 与既有 main/content/contents 状态报告，已删除账号的历史任务保留；删除账号不会隐式关闭分工或让原任务切换身份。Web 兼容过滤旧响应中的 missing 行，从未保存的选择和并发配置中移除缺失 ID，保留其他编辑；删除成功后立即去掉列表行，即使后续刷新失败。主账号或必需功能缺失时，用户需重新选择并保存配置。

App 漫画阅读器按 asset.position 排序，提供横向翻页与竖向连续瀑布流。后者通过两段懒加载 Sliver 围绕恢复页向上下延伸，不预取所有前页；保存零基页码与 offset(0–1) 的页内比例。图片进度仅允许 section=0，小说章节校验保持。阅读方式存于设备安全存储；混合正文和小说不展示图像模式菜单。详情不自动加载正文图像。瀑布流图片不覆盖放大按钮，双击进入支持双指缩放的全屏查看；普通单击不打开，纵向拖动仍滚动，关闭后保留页码和页内偏移。辅助功能可通过图片的语义操作打开缩放。

详情工作进程每 60 秒扫描缺封面的 Pixiv 小说（含标记为 article 的 novel:* 来源），每次只读取 200 条候选的标识，通过游标继续扫描，最多维持 20 个待执行／执行中的自动封面任务。任务为 import + download=false + auto_novel_cover=true，沿用详情账号池、队列和账号并发、总暂停、详情暂停与请求退避。未配置 Pixiv 账号时不排队；同作品已有采集任务时不重复创建。自动任务终止后至少 24 小时才复用原任务和账号分配重试，封面暂不可用但任务完成也遵守冷却。用户升级为正文下载的任务退出自动补全语义。执行前及提交时检查封面，已删除或已补封面的作品跳过；正文、人工资料、收藏、评分及阅读进度保留。无可追溯远端来源的本地小说不自动匹配网络封面。

LibraryScreen 打开详情后不再在 pop 时重新 GET /items；详情的读取、下载和汉化更新通过 onChanged 替换同 ID 本地条目。ReaderScreen 在进度 PUT 成功后发出 onProgressSaved，详情以不可变副本更新进度，不在返回时重新 GET 详情。失败或放弃的未保存位置不被当作已保存进度。人物墙回退保留列表和分页，人物资料确认变更通过回调局部替换。主动下拉刷新和筛选变更仍重新获取数据。

## 收藏、分组、评分与历史

迁移 c86fe430a912 在 b58ce962d713 后增加 favorite_groups、item_favorites、favorite_group_items、item_visits 四表，以及 items.user_score(默认0)、items.source_bookmark_count(可空)。迁移仅首次创建「想看、在看、已看、推荐」四组，删除后不自动重建。单管理员书库内共享数据，不调用远端平台收藏接口。

GET /favorite-groups 返回 items 分组数组、总收藏数和未分组数。POST 同路径创建、PUT /favorite-groups/{id} 重命名、DELETE 同路径仅删除分组及其关联，保留作品收藏。名称规范化、去除首尾空白，最长80字符、唯一。PUT /items/{id}/favorite 接受严格布尔 favorite、可选 group_ids(多选)。省略 group_ids 保留已有分组，[] 为未分组；取消收藏清理关联但保留评分。旧客户端发送的可选 score(严格整数0–5)仍兼容，无效分组和评分整体拒绝，不部分保存。新版 Web/App 收藏请求不发送 score，收藏编辑器不显示评分。行锁与主键保护重复请求和并发更新。

PUT /items/{id}/score 接受必填严格整数 score(0–5)，0 为清除，返回 {id,user_score}，仅更新评分。未收藏作品也可评分，不创建收藏或修改分组。Web/App 提供独立评分入口，保存结果仅合并对应字段；失败保留原数据和输入，允许重试。手机 Bearer 允许该精确 PUT 路径，不扩展任意作品写入权限；旧 App 可继续连接新版服务器。无需数据库迁移。

GET /items 新增 favorite=true、favorite_group={id|ungrouped}、history=true，可组合原有搜索／平台／类型／已下载条件与分页。历史以 viewed_at 降序和 item.id 排序；POST /items/{id}/visit 对同一作品更新一条最近访问记录，DELETE /history 清空记录而不影响阅读位置、收藏及原文件。作品删除通过外键级联清理收藏和历史。详情视图新增 is_favorite、favorite_group_ids、user_score、source_bookmark_count。

手机 Bearer 只扩展上述精确路径的读写、分组删除与清空历史，以及 POST /items/{id}/cover-refresh；不授予作品删除、任意 PATCH、账号或队列设置权限。原鉴权、设备撤销、Origin 校验保持。封面刷新复用详情队列、原限速和账号功能分配，仅为 Pixiv 排队 download=false；已下载小说可补封面，原文、评分、收藏和进度保持。

Pixiv total_bookmarks 使用非负整数记录；EH filecount、PicACG pagesCount、JM SDK page_count 和 Pixiv page_count 在元数据阶段保存总页数。缺失或不合法数据不伪造为 0；其他平台未返回真实收藏人数时保持 null。Pixiv 小说合并下载另存 preview_cover。图像列表在无正文时可使用 remote_page_count 展示 P 数；实际阅读仍由 content_state 与 assets 判定。

## 小说正文插图与标签追踪

迁移 e71b4c8213de 在 assets 添加可空 inline_key、width、height。Pixiv 下载保留原始 UTF-8 TXT，识别 uploadedimage 和 pixivimage（含分 P）标记，只下载受信任的 pximg.net HTTPS 图片；重复引用复用请求。正文插图与封面独立，图片按文件验证、暂存和完整导入，受单文件、总量、500 张插图与队列取消限制。已有正文以 novel_images_only 下载任务追加插图，不重新合并标题、收藏、评分、标签或进度；原 TXT 不覆盖。

GET /assets/{id}/text-sections 保留原 index/title/text，新增 blocks：纯文本块或 image 块。图片只解析为同一作品的本地资产 URL 和已验证宽高，无法解析的引用保留 unavailable 占位。详情的 assets 仍为独立阅读文件，inline_assets 单独列出正文插图；page_count 不增加，size_bytes 和作品 ZIP 导出包含插图。旧客户端不会将插图当作额外文件翻页。Web 与 Flutter 按原位置混排；Flutter 继续以已测量高度虚拟化章节，图片加载或失败重试不改变滚动长度。Pixiv 直连阅读使用手机主账号读取插图，缺失图片不阻止阅读文字。

GET/POST /tag-tracking 复用 target_type=tag 的订阅；POST 接受 platform（pixiv/picacg/kemono）、1–20 个原文 tags、auto_download（默认 true）、content_scope（Pixiv 默认 all）、interval_minutes（30–10080）。标签去空白、NFC 规范化并去重，同平台同标签复用已有订阅，活跃检查任务合并。每次检查平台最新一页，不承诺完整历史回溯；不同标签独立追踪，不将本地标签翻译作为远端搜索词。

PATCH/DELETE /tag-tracking/{id} 仅操作标签订阅，PATCH 仅允许启停、自动下载、范围与间隔；POST /tag-tracking/{id}/check 在原检查队列排队。Pixiv 检索使用主账号，发现作品的正文进入原自动下载队列，保持主副账号功能、每账号与各队列并发限制。关闭追踪或取消追踪保留已入库作品，不取消此前已经排队的下载。手机阅读凭据仅扩展精确的标签追踪接口，不获得通用订阅、账号或队列管理权限。

## 服务器性能面板

GET /api/server/performance 只允许管理员 Cookie，普通扫码设备凭据不授予该接口。响应为内存缓存快照：status、sample_age_seconds、current、history、database、storage；首次或计数器复位时速率为 null，不伪造为零。重启清空趋势，保留最多 150 个两秒采样点，不新增数据库表。

CPU 和内存区分当前操作系统整体与书库服务。Docker 使用 cgroup v2／v1 配额、CPU 累计时间及内存工作集（当前使用减 inactive_file）；非容器使用 API 和后代进程的 CPU／RSS 合计，RSS 可能重复计共享页。整体 CPU 个数不由已绑定单核的 API 进程亲和性推断。单进程 CPU 以一个逻辑核为 100%，列出 PID、队列、状态、绑定核心及重启次数，不返回命令行、环境变量、数据库 URL 或存储路径。

系统采样每两秒；数据库独立线程每十秒 SELECT 1 并只汇总各 lane 的 queued/running，利用任务领取索引，不统计全部历史任务；存储独立线程每三十秒 statvfs，只读取磁盘容量，不递归扫描书库。请求路径不执行采样、SQL 或 NAS I/O；数据库、存储错误和过期单独标记。容器网络仅统计当前网络命名空间的非回环接口，不包含宿主机 SMB 挂载传输；服务资源不含独立 MySQL 容器。

Web #performance 与 App 管理工作台提供只读原生面板。两秒轮询、连续失败退避至十秒，保留最后成功数据，支持暂停；页面隐藏／App 后台／离页停止轮询，迟到响应不能覆盖新页面。

## 队列暂停与等待原因

GET /queue/status 的各 lane 增加 diagnostic={state,code,reasons}，只读解释当前状态，不领取任务、切换账号或自动恢复开关。state 为 paused/waiting/running/idle/ready。暂停只表示本队列开关或采集总开关；手动下载、失败重试及 AI 等独立队列不受采集总暂停影响。本队列和总开关同时暂停时返回两条原因及各自恢复方法。

PUT /queue/settings 与 PUT /queue/lanes/{lane} 保持原响应，新增可选 pause_reason=manual/maintenance，仅在 paused=true 时接受。实际开关转换记录 UTC 时间和来源；重复提交同状态不重写原始原因，改并发或其他队列也保留记录。元数据复用 queue_control.lanes JSON：各 lane.pause_event 和 _collection_pause，不新增表。旧暂停没有来源时返回 source=legacy、changed_at=null，并明确说明旧版未记录，不将旧暂停归因于管理员、维护、限流或故障。

等待诊断区分平台冷却及预计恢复时间、队列并发占满、账号共享并发占满、工作进程未启用／启动／重启／心跳超时。账号诊断只在网络队列尚有等待任务且未达队列并发上限时，复用领取时的身份容量 SQL 条件检查是否存在可执行任务，不解密账号令牌或改变任务分配。空队列显示无待执行任务，历史失败记录不会自动当作待执行任务。SIGKILL 或进程重启不直接推断为 OOM。
