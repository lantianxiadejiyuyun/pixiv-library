use crate::{AppState, db::json_column, error::Result, queue, runtime};
use axum::{Json, extract::State};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    time::{Duration, Instant},
};
use tokio_util::sync::CancellationToken;
fn stamp() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}
fn pct(u: u64, t: u64) -> f64 {
    if t == 0 {
        0.0
    } else {
        (u as f64 / t as f64 * 10000.0).clamp(0.0, 10000.0).round() / 100.0
    }
}
pub struct Monitor {
    stop: CancellationToken,
    handle: tokio::task::JoinHandle<()>,
}
impl Monitor {
    pub fn start(s: AppState) -> Self {
        let stop = CancellationToken::new();
        let token = stop.clone();
        let handle = tokio::spawn(async move {
            let mut system = sysinfo::System::new_all();
            let mut networks = sysinfo::Networks::new_with_refreshed_list();
            let mut history = VecDeque::new();
            let born = Instant::now();
            let mut previous = Instant::now();
            let mut last_db = Instant::now() - Duration::from_secs(30);
            let mut database = Value::Null;
            let mut storage_job = Some(storage_probe(s.clone()));
            let mut storage = serde_json::json!([]);
            let mut storage_at = Instant::now() - Duration::from_secs(3600);
            loop {
                if token.is_cancelled() {
                    break;
                }
                let state = s.clone();
                let captured=tokio::task::spawn_blocking(move||{system.refresh_all();networks.refresh(true);let runtime=state.runtime.read().map(|v|v.clone()).unwrap_or(Value::Null);let mut processes=vec![];let mut native_cpu=0.0;let mut rss=0u64;let mut wanted=vec![(std::process::id(),"api".to_owned(),json!(runtime::available().unwrap_or_default()),"running".to_owned(),json!(0))];for w in runtime["workers"].as_array().into_iter().flatten(){if let Some(pid)=w["pid"].as_u64(){wanted.push((pid as u32,w["lane"].as_str().unwrap_or("worker").into(),w["cpus"].clone(),w["state"].as_str().unwrap_or("unknown").into(),w["restarts"].clone()));}}
                    for (pid,lane,cpus,status,restarts) in wanted{let p=system.process(sysinfo::Pid::from_u32(pid));let cpu=p.map(|p|p.cpu_usage() as f64);let memory=p.map(|p|p.memory());native_cpu+=cpu.unwrap_or(0.0);rss+=memory.unwrap_or(0);processes.push(json!({"pid":pid,"lane":lane,"cpus":cpus,"state":status,"restarts":restarts,"cpu_percent":cpu,"rss_bytes":memory}));}let logical=system.cpus().len();let capacity=std::fs::read_to_string("/sys/fs/cgroup/cpu.max").ok().and_then(|v|{let p=v.split_whitespace().collect::<Vec<_>>();if p.len()==2{Some(p[0].parse::<f64>().ok()?/p[1].parse::<f64>().ok()?)}else{None}}).filter(|n|n.is_finite()&&*n>0.0).unwrap_or(logical.max(1) as f64).min(cpu_capacity(&runtime).max(1) as f64);
                    let host_total=system.total_memory();let host_available=system.available_memory();let cg=std::path::Path::new("/.dockerenv").exists()||std::path::Path::new("/run/.containerenv").exists();let memory_limit=cgroup_number("memory.max").filter(|n|*n<host_total).unwrap_or(host_total);let current=if cg{cgroup_number("memory.current").unwrap_or(rss)}else{rss};let inactive=cgroup_values("memory.stat").get("inactive_file").copied().unwrap_or(0);let working=if cg{current.saturating_sub(inactive)}else{rss};let elapsed=previous.elapsed().as_secs_f64().max(0.001);let rx=networks.iter().filter(|(name,_)|name.as_str()!="lo").map(|(_,n)|n.received()).sum::<u64>() as f64/elapsed;let tx=networks.iter().filter(|(name,_)|name.as_str()!="lo").map(|(_,n)|n.transmitted()).sum::<u64>() as f64/elapsed;let service_cpu=(native_cpu/capacity).min(100.0);let at=chrono::Utc::now().timestamp_millis() as f64/1000.0;let sample=json!({"at":at,"host_cpu":system.global_cpu_usage(),"service_cpu":service_cpu,"host_memory":pct(host_total-host_available,host_total),"service_memory":pct(working,memory_limit),"download_bps":rx,"upload_bps":tx});let current=json!({"sampled_at":stamp(),"platform":std::env::consts::OS,"container":cg,"cpu":{"host_percent":system.global_cpu_usage(),"service_percent":service_cpu,"logical_cpus":logical,"service_capacity":capacity,"load_average":[sysinfo::System::load_average().one,sysinfo::System::load_average().five,sysinfo::System::load_average().fifteen],"throttled_seconds":cgroup_values("cpu.stat").get("throttled_usec").map(|v|*v as f64/1e6)},"memory":{"host":{"total_bytes":host_total,"available_bytes":host_available,"used_bytes":host_total-host_available,"percent":pct(host_total-host_available,host_total)},"service":{"used_bytes":working,"current_bytes":current,"limit_bytes":memory_limit,"percent":pct(working,memory_limit),"basis":if cg{"cgroup_working_set"}else{"process_rss_sum"},"oom_kills":cgroup_values("memory.events").get("oom_kill")}},"network":{"download_bps":rx,"upload_bps":tx,"scope":if cg{"container"}else{"host"}},"uptime_seconds":sysinfo::System::uptime(),"service_uptime_seconds":born.elapsed().as_secs(),"processes":processes,"worker_mode":runtime["mode"]});(system,networks,current,sample)}).await;
                let Ok((sys, net, current, sample)) = captured else {
                    break;
                };
                system = sys;
                networks = net;
                previous = Instant::now();
                history.push_back(sample);
                if history.len() > 150 {
                    history.pop_front();
                }
                if last_db.elapsed() > Duration::from_secs(10) {
                    let start = Instant::now();
                    database = match tokio::time::timeout(
                        Duration::from_secs(3),
                        s.db.query("SELECT 1", &[]),
                    )
                    .await
                    {
                        Ok(Ok(_)) => {
                            let row =
                                s.db.one("SELECT * FROM queue_control WHERE id=1", &[])
                                    .await
                                    .ok()
                                    .flatten()
                                    .unwrap_or(Value::Null);
                            let mut row = row;
                            row["lanes"] = json_column(&row["lanes"]);
                            let counts=s.db.query("SELECT queue_lane,status,COUNT(*) AS n FROM jobs WHERE status IN ('queued','running') GROUP BY queue_lane,status",&[]).await.unwrap_or_default();
                            let queues=queue::LANES.iter().map(|(id,label)|{let settings=queue::lane(&row,id);json!({"id":id,"label":label,"queued":counts.iter().find(|c|c["queue_lane"]==*id&&c["status"]=="queued").map(|c|c["n"].clone()).unwrap_or(json!(0)),"running":counts.iter().find(|c|c["queue_lane"]==*id&&c["status"]=="running").map(|c|c["n"].clone()).unwrap_or(json!(0)),"paused":settings["paused"],"concurrency":settings["concurrency"],"effective_paused":settings["paused"]==true||row["paused"]==true&&queue::collection(id)})}).collect::<Vec<_>>();
                            json!({"status":"ok","latency_ms":start.elapsed().as_secs_f64()*1000.0,"queues":queues,"pool":null,"stale":false})
                        }
                        _ => {
                            json!({"status":"unavailable","latency_ms":null,"queues":[],"pool":null,"stale":false})
                        }
                    };
                    last_db = Instant::now();
                }
                if storage_job.as_ref().is_some_and(|job| job.is_finished())
                    && let Some(job) = storage_job.take()
                    && let Ok(value) = job.await
                {
                    storage = value;
                    storage_at = Instant::now();
                }
                if storage_job.is_none() && storage_at.elapsed() > Duration::from_secs(60) {
                    storage_job = Some(storage_probe(s.clone()));
                }
                if let Ok(mut telemetry) = s.telemetry.write() {
                    *telemetry = json!({"status":"ready","refresh_seconds":2,"sample_age_seconds":0,"current":current,"history":history,"database":database,"storage":storage,"storage_stale":storage_at.elapsed()>Duration::from_secs(90)});
                }
                tokio::select! {_=token.cancelled()=>break,_=tokio::time::sleep(Duration::from_secs(2))=>()}
            }
        });
        Self { stop, handle }
    }
    pub async fn stop(self) {
        self.stop.cancel();
        let _ = tokio::time::timeout(Duration::from_secs(3), self.handle).await;
    }
}
fn cgroup_number(file: &str) -> Option<u64> {
    std::fs::read_to_string(format!("/sys/fs/cgroup/{file}"))
        .ok()?
        .trim()
        .parse()
        .ok()
}
fn cgroup_values(file: &str) -> std::collections::HashMap<String, u64> {
    std::fs::read_to_string(format!("/sys/fs/cgroup/{file}"))
        .unwrap_or_default()
        .lines()
        .filter_map(|line| {
            let (k, v) = line.split_once(' ')?;
            Some((k.to_owned(), v.trim().parse().ok()?))
        })
        .collect()
}
pub async fn get(State(s): State<AppState>) -> Result<Json<Value>> {
    Ok(Json(
        s.telemetry
            .read()
            .map(|v| v.clone())
            .unwrap_or_else(|_| json!({"status":"unavailable"})),
    ))
}

fn cpu_capacity(runtime: &Value) -> usize {
    let mut ids = runtime::available()
        .unwrap_or_default()
        .into_iter()
        .collect::<std::collections::HashSet<_>>();
    for worker in runtime["workers"].as_array().into_iter().flatten() {
        for id in worker["cpus"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_u64)
        {
            ids.insert(id as usize);
        }
    }
    ids.len()
}
fn storage_probe(s: AppState) -> tokio::task::JoinHandle<Value> {
    // At most one probe is outstanding. A hung SMB stat cannot block API,
    // CPU sampling or create an unbounded stream of replacement threads.
    tokio::task::spawn_blocking(move || {
        let disks = sysinfo::Disks::new_with_refreshed_list();
        let root = s.config.storage_root.clone();
        let system = std::env::current_dir()
            .ok()
            .and_then(|p| p.ancestors().last().map(std::path::Path::to_path_buf))
            .unwrap_or_else(|| "/".into());
        let rows=[("system","系统盘",system),("library","书库存储 / NAS",root)].into_iter().map(|(id,label,path)|{
            let mut row=json!({"id":id,"label":label,"status":"unavailable"});
            if id=="library"&&s.config.require_marker&&!path.join(".library-storage").is_file(){return row;}
            if let Some(disk)=disks.iter().filter(|d|path.starts_with(d.mount_point())).max_by_key(|d|d.mount_point().as_os_str().len()){
                let total=disk.total_space();let free=disk.available_space();if total>0{row=json!({"id":id,"label":label,"status":"ok","total_bytes":total,"used_bytes":total.saturating_sub(free),"free_bytes":free,"percent":pct(total.saturating_sub(free),total)});}
            }row
        }).collect::<Vec<_>>();
        json!(rows)
    })
}
