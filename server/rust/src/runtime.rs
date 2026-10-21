//! Native worker processes. Each lane has its own runtime and CPU placement;
//! QueueControl and lease ownership remain the authority across all processes.
use crate::{
    AppState,
    config::Config,
    db::now,
    error::{ApiError, Result},
    queue, tasks,
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::{
    process::{Child, Command},
    task::JoinSet,
};
use tokio_util::sync::CancellationToken;
pub const ROLES: [&str; 9] = [
    "api",
    "download",
    "details",
    "checks",
    "ai",
    "translation",
    "other",
    "manual_download",
    "retry",
];
pub fn available() -> Result<Vec<usize>> {
    #[cfg(target_os = "linux")]
    unsafe {
        let mut set: libc::cpu_set_t = std::mem::zeroed();
        if libc::sched_getaffinity(0, std::mem::size_of_val(&set), &mut set) != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        Ok((0..libc::CPU_SETSIZE as usize)
            .filter(|n| libc::CPU_ISSET(*n, &set))
            .collect())
    }
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::System::Threading::*;
        let (mut process, mut system) = (0usize, 0usize);
        if GetProcessAffinityMask(GetCurrentProcess(), &mut process, &mut system) == 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        return Ok((0..usize::BITS as usize)
            .filter(|i| process & (1usize << i) != 0)
            .collect());
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    Ok((0..std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1))
        .collect())
}
pub fn apply(cpus: &[usize]) -> Result<bool> {
    if cpus.is_empty() {
        return Ok(false);
    }
    #[cfg(target_os = "linux")]
    unsafe {
        let mut set: libc::cpu_set_t = std::mem::zeroed();
        libc::CPU_ZERO(&mut set);
        for n in cpus {
            if *n >= libc::CPU_SETSIZE as usize {
                return Err(ApiError::bad("CPU 编号超出范围"));
            }
            libc::CPU_SET(*n, &mut set);
        }
        if libc::sched_setaffinity(0, std::mem::size_of_val(&set), &set) != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        Ok(true)
    }
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::System::Threading::*;
        let mut mask = 0usize;
        for n in cpus {
            if *n >= usize::BITS as usize {
                return Err(ApiError::bad("CPU 编号超出范围"));
            }
            mask |= 1usize << n;
        }
        if SetProcessAffinityMask(GetCurrentProcess(), mask) == 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        return Ok(true);
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    Ok(false)
}
pub fn plan(cpus: &[usize], setting: &str) -> Result<BTreeMap<String, Vec<usize>>> {
    if cpus.is_empty() {
        return Err(ApiError::bad("没有可用的 CPU"));
    }
    if setting == "off" {
        return Ok(ROLES.iter().map(|r| (r.to_string(), vec![])).collect());
    }
    if setting != "auto" {
        let mut v: BTreeMap<String, Vec<usize>> = serde_json::from_str(setting)
            .map_err(|_| ApiError::bad("CPU 配置须为 auto、off 或各队列的有效 CPU 编号 JSON"))?;
        for role in ["manual_download", "retry"] {
            if !v.contains_key(role) {
                v.insert(role.into(), v.get("download").cloned().unwrap_or_default());
            }
        }
        if v.len() != ROLES.len()
            || ROLES.iter().any(|r| !v.contains_key(*r))
            || v.values().any(|ids| {
                ids.is_empty()
                    || ids.iter().collect::<std::collections::HashSet<_>>().len() != ids.len()
                    || ids.iter().any(|i| !cpus.contains(i))
            })
        {
            return Err(ApiError::bad(
                "CPU 配置包含缺失队列、重复或不可用的 CPU 编号",
            ));
        }
        return Ok(v);
    }
    let reserve = if cpus.len() >= 14 { 2 } else { 1 };
    let mut v = BTreeMap::from([("api".into(), cpus[..reserve].to_vec())]);
    let rest = if cpus.len() > reserve {
        &cpus[reserve..]
    } else {
        cpus
    };
    if rest.len() < 8 {
        for (i, role) in ROLES[1..].iter().enumerate() {
            v.insert(role.to_string(), vec![rest[i % rest.len()]]);
        }
    } else {
        let width = rest.len() / 8;
        for (i, role) in ROLES[1..].iter().enumerate() {
            v.insert(role.to_string(), rest[i * width..(i + 1) * width].to_vec());
        }
        v.get_mut("ai")
            .unwrap()
            .extend_from_slice(&rest[8 * width..]);
    }
    Ok(v)
}
pub struct Placement {
    pub cpus: BTreeMap<String, Vec<usize>>,
    pub applied: bool,
}
pub fn prepare() -> Result<Placement> {
    let cpus = available()?;
    let setting = std::env::var("LIBRARY_CPU_AFFINITY").unwrap_or_else(|_| "auto".into());
    let mut cpus = plan(&cpus, &setting)?;
    let applied = match apply(&cpus["api"]) {
        Ok(v) => v,
        Err(_) if setting == "auto" => {
            cpus = plan(&available()?, "off")?;
            false
        }
        Err(e) => return Err(e),
    };
    Ok(Placement { cpus, applied })
}
pub fn enabled(c: &Config) -> bool {
    !c.read_only
        && std::env::var("LIBRARY_RUST_WORKERS_ENABLED")
            .is_ok_and(|v| matches!(v.as_str(), "true" | "1" | "yes"))
}
fn epoch() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}
struct LaneProcess {
    child: Option<Child>,
    restarts: u32,
    next: Instant,
    started: Instant,
}
fn read_status(dir: &Path, lane: &str) -> Value {
    std::fs::read(dir.join(format!("{lane}.json")))
        .ok()
        .filter(|b| b.len() < 16384)
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or(Value::Null)
}
pub struct Supervisor {
    stop: CancellationToken,
    handle: tokio::task::JoinHandle<()>,
}
impl Supervisor {
    pub fn start(s: AppState, placement: Placement) -> Result<Self> {
        s.config.writable()?;
        let directory = tempfile::Builder::new()
            .prefix("qiye-rust-workers-")
            .tempdir()?;
        let executable = std::env::current_exe()?;
        let stop = CancellationToken::new();
        let token = stop.clone();
        let handle = tokio::spawn(async move {
            let mut workers: BTreeMap<String, LaneProcess> = queue::LANES
                .iter()
                .map(|(lane, _)| {
                    (
                        lane.to_string(),
                        LaneProcess {
                            child: None,
                            restarts: 0,
                            next: Instant::now(),
                            started: Instant::now(),
                        },
                    )
                })
                .collect();
            loop {
                if token.is_cancelled() {
                    break;
                }
                let mut rows = vec![];
                for (lane, row) in &mut workers {
                    let mut exited = false;
                    if let Some(child) = &mut row.child
                        && !matches!(child.try_wait(), Ok(None))
                    {
                        exited = true;
                    }
                    if exited {
                        let status = read_status(directory.path(), lane);
                        if let Some(owners) = status["owners"].as_array() {
                            let _ = recover_owners(&s, owners).await;
                        }
                        row.child.take();
                        row.restarts += 1;
                        row.next = Instant::now()
                            + Duration::from_secs((1u64 << row.restarts.min(5)).min(30));
                    }
                    if row.child.is_none() && Instant::now() >= row.next {
                        let mut cmd = Command::new(&executable);
                        cmd.arg("worker")
                            .arg(lane)
                            .env("LIBRARY_WORKER_PROCESS", lane)
                            .env(
                                "LIBRARY_WORKER_CPUS",
                                serde_json::to_string(&placement.cpus[lane]).unwrap(),
                            )
                            .env("LIBRARY_WORKER_PARENT_PID", std::process::id().to_string())
                            .env("LIBRARY_WORKER_STATUS_DIR", directory.path())
                            .env("LIBRARY_RUST_WORKERS_ENABLED", "false")
                            .env("LIBRARY_RUST_DB_CONNECTIONS", "4")
                            .stdin(std::process::Stdio::null())
                            .kill_on_drop(true);
                        #[cfg(windows)]
                        cmd.creation_flags(0x08000000);
                        match cmd.spawn() {
                            Ok(child) => {
                                row.child = Some(child);
                                row.started = Instant::now();
                            }
                            Err(_) => {
                                row.restarts += 1;
                                row.next = Instant::now() + Duration::from_secs(30);
                            }
                        }
                    }
                    let mut status = read_status(directory.path(), lane);
                    let alive = row.child.is_some();
                    let fresh = status["pid"].as_u64()
                        == row.child.as_ref().and_then(|c| c.id()).map(u64::from)
                        && status["heartbeat"]
                            .as_f64()
                            .is_some_and(|v| epoch() - v < 10.0);
                    if !status.is_object() {
                        status = json!({});
                    }
                    status["lane"] = json!(lane);
                    status["running"] = json!(alive && fresh);
                    status["restarts"] = json!(row.restarts);
                    status["status"] = json!(if !alive {
                        "restarting"
                    } else if fresh {
                        "running"
                    } else {
                        "starting"
                    });
                    status["cpus"] = json!(placement.cpus[lane]);
                    status["state"] = status["status"].clone();
                    if alive && !fresh && row.started.elapsed() > Duration::from_secs(30) {
                        if let Some(child) = row.child.as_mut() {
                            let _ = child.start_kill();
                        }
                        status["state"] = json!("unresponsive");
                        status["status"] = json!("unresponsive");
                    }
                    rows.push(status);
                }
                let mut seen = std::collections::HashSet::new();
                let shared = placement.cpus.values().flatten().any(|i| !seen.insert(i));
                if let Ok(mut runtime) = s.runtime.write() {
                    *runtime = json!({"mode":"process","workers":rows,"api_cpus":placement.cpus["api"],"shared_cpus":shared,"affinity_applied":placement.applied,"workers_migrated":true});
                }
                tokio::select! {_=token.cancelled()=>break,_=tokio::time::sleep(Duration::from_secs(1))=>()}
            }
            let _ = tokio::fs::write(directory.path().join("stop"), b"stop").await;
            let deadline = Instant::now() + Duration::from_secs(8);
            for row in workers.values_mut() {
                if let Some(mut child) = row.child.take() {
                    let left = deadline.saturating_duration_since(Instant::now());
                    if tokio::time::timeout(left, child.wait()).await.is_err() {
                        let _ = child.kill().await;
                        let _ = child.wait().await;
                    }
                }
            }
            for lane in workers.keys() {
                let status = read_status(directory.path(), lane);
                if let Some(owners) = status["owners"].as_array() {
                    let _ = recover_owners(&s, owners).await;
                }
            }
            if let Ok(mut status) = s.runtime.write() {
                *status = json!({"mode":"disabled","workers":[],"workers_migrated":true});
            }
        });
        Ok(Self { stop, handle })
    }
    pub async fn stop(self) {
        self.stop.cancel();
        let _ = self.handle.await;
    }
}
async fn recover_owners(s: &AppState, owners: &[Value]) -> Result<()> {
    let owners: Vec<_> = owners
        .iter()
        .filter_map(Value::as_str)
        .filter(|id| uuid::Uuid::parse_str(id).is_ok())
        .take(8)
        .collect();
    if owners.is_empty() {
        return Ok(());
    }
    let mut tx = s.db.begin().await?;
    queue::locked(s, &mut tx).await?;
    for owner in owners {
        let rows = tx
            .query(
                "SELECT id FROM jobs WHERE worker_id=? AND status='running'",
                &[json!(owner)],
            )
            .await?;
        for row in rows {
            tx.execute("UPDATE jobs SET status='queued',lease_until=NULL,worker_id=NULL,updated_at=? WHERE id=? AND worker_id=? AND status='running'",&[json!(now()),row["id"].clone(),json!(owner)]).await?;
            tasks::sync_state(&mut tx, row["id"].as_str().unwrap()).await?;
        }
    }
    tx.commit().await?;
    Ok(())
}
pub async fn worker(s: AppState, lane: &str) -> Result<()> {
    s.config.writable()?;
    if !queue::LANES.iter().any(|(id, _)| *id == lane) {
        return Err(ApiError::bad("无效的队列名称"));
    }
    let parent = std::env::var("LIBRARY_WORKER_PARENT_PID")
        .ok()
        .and_then(|v| v.parse::<u32>().ok())
        .filter(|n| *n > 0)
        .ok_or_else(|| ApiError::bad("缺少队列父进程"))?;
    let dir = PathBuf::from(
        std::env::var_os("LIBRARY_WORKER_STATUS_DIR")
            .ok_or_else(|| ApiError::bad("缺少队列状态目录"))?,
    );
    if !dir.is_dir() {
        return Err(ApiError::bad("队列状态目录不存在"));
    }
    let stop = CancellationToken::new();
    let signal_stop = stop.clone();
    let watch_stop = stop.clone();
    let watch_dir = dir.clone();
    tokio::spawn(async move {
        loop {
            if !parent_alive(parent) {
                watch_stop.cancel();
                tokio::time::sleep(Duration::from_secs(7)).await;
                std::process::exit(0);
            }
            if watch_dir.join("stop").exists() {
                watch_stop.cancel();
                break;
            }
            if watch_stop.is_cancelled() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
    });
    tokio::spawn(async move {
        shutdown_signal().await;
        signal_stop.cancel();
    });
    let executor = Arc::new(crate::executor::NativeExecutor);
    let mut active = JoinSet::new();
    let mut last_schedule = Instant::now() - Duration::from_secs(10);
    let mut last_cover = Instant::now() - Duration::from_secs(65);
    let mut cover_cursor = None;
    let mut owners = std::collections::HashSet::new();
    loop {
        while let Some(done) = active.try_join_next() {
            if let Ok(owner) = done {
                owners.remove(&owner);
            }
        }
        if !parent_alive(parent) || dir.join("stop").exists() {
            stop.cancel();
        }
        publish(&dir, lane, &owners, stop.is_cancelled()).await?;
        if stop.is_cancelled() {
            break;
        }
        if lane == "checks" && last_schedule.elapsed() >= Duration::from_secs(5) {
            let _ = crate::acquisition::tick(&s).await;
            last_schedule = Instant::now();
        }
        if lane == "details" && last_cover.elapsed() >= Duration::from_secs(60) {
            let _ = crate::cover_scheduler::tick(&s, &mut cover_cursor).await;
            last_cover = Instant::now();
        }
        while active.len() < 8 {
            match tasks::claim(&s, lane).await {
                Ok(Some(claim)) => {
                    let owner = claim.owner.clone();
                    owners.insert(owner.clone());
                    publish(&dir, lane, &owners, false).await?;
                    let state = s.clone();
                    let executor = executor.clone();
                    let stop = stop.clone();
                    active.spawn(async move {
                        let _ = tasks::execute(state, claim, executor, stop).await;
                        owner
                    });
                }
                Ok(None) => break,
                Err(_) => {
                    tracing::warn!(lane, "队列暂时无法访问数据库");
                    break;
                }
            }
        }
        tokio::select! {_=stop.cancelled()=>break,_=tokio::time::sleep(Duration::from_millis(if active.is_empty(){1000}else{250}))=>()}
    }
    stop.cancel();
    let _ = tokio::time::timeout(Duration::from_secs(5), async {
        while active.join_next().await.is_some() {}
    })
    .await;
    active.abort_all();
    publish(&dir, lane, &owners, true).await?;
    Ok(())
}
async fn publish(
    dir: &Path,
    lane: &str,
    owners: &std::collections::HashSet<String>,
    stopped: bool,
) -> Result<()> {
    let data = json!({"pid":std::process::id(),"heartbeat":epoch(),"cpus":available().unwrap_or_default(),"active":owners.len(),"owners":owners,"stopped":stopped});
    let temporary = dir.join(format!("{lane}.partial"));
    tokio::fs::write(&temporary, data.to_string()).await?;
    tokio::fs::rename(temporary, dir.join(format!("{lane}.json"))).await?;
    Ok(())
}
fn parent_alive(pid: u32) -> bool {
    #[cfg(unix)]
    unsafe {
        libc::getppid() as u32 == pid
    }
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::{Foundation::CloseHandle, System::Threading::*};
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return false;
        }
        let mut code = 0;
        let alive = GetExitCodeProcess(handle, &mut code) != 0 && code == 259;
        CloseHandle(handle);
        return alive;
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = pid;
        true
    }
}
pub async fn shutdown_signal() {
    #[cfg(unix)]
    {
        if let Ok(mut signal) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            tokio::select! {_=signal.recv()=>(),_=tokio::signal::ctrl_c()=>()};
            return;
        }
    }
    let _ = tokio::signal::ctrl_c().await;
}
