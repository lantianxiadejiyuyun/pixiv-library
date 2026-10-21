use qiye_server::{config::Config, connect, error::Result, router, runtime};
use tracing_subscriber::EnvFilter;
fn main() {
    if let Err(e) = entry() {
        eprintln!("{}", e.1);
        std::process::exit(1);
    }
}
fn entry() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| "qiye_server=info".into()),
        )
        .init();
    let config = Config::from_env()?;
    let args: Vec<_> = std::env::args().skip(1).collect();
    let worker = args.first().is_some_and(|a| a == "worker");
    let migrate = args.first().is_some_and(|a| a == "migrate");
    if !args.is_empty() && !worker && !migrate {
        return Err(qiye_server::error::ApiError::bad(
            "命令须为 migrate、worker 队列名，或留空启动 API",
        ));
    }
    let placement = if worker {
        let cpus: Vec<usize> = serde_json::from_str(
            &std::env::var("LIBRARY_WORKER_CPUS").unwrap_or_else(|_| "[]".into()),
        )
        .map_err(|_| qiye_server::error::ApiError::bad("队列 CPU 配置无效"))?;
        runtime::apply(&cpus)?;
        None
    } else if runtime::enabled(&config) {
        Some(runtime::prepare()?)
    } else {
        None
    };
    let threads = runtime::available()?.len().clamp(1, 4);
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(threads)
        .enable_all()
        .build()?;
    let result = rt.block_on(async move {
        let bind = config.bind;
        if migrate {
            let db = qiye_server::db::Db::connect(&config).await?;
            return qiye_server::migrations::initialize(&config, &db).await;
        }
        let state = connect(config).await?;
        qiye_server::migrations::verify(&state.config, &state.db).await?;
        if worker {
            let lane = args
                .get(1)
                .ok_or_else(|| qiye_server::error::ApiError::bad("缺少队列名称"))?;
            return runtime::worker(state, lane).await;
        }
        let supervisor = placement
            .map(|p| runtime::Supervisor::start(state.clone(), p))
            .transpose()?;
        let monitor = qiye_server::performance::Monitor::start(state.clone());
        let proxy = qiye_server::managed_proxy::Manager::start(state.clone());
        let listener = tokio::net::TcpListener::bind(bind).await?;
        tracing::info!(%bind,"Rust API listening");
        let result = axum::serve(
            listener,
            router(state).into_make_service_with_connect_info::<std::net::SocketAddr>(),
        )
        .with_graceful_shutdown(runtime::shutdown_signal())
        .await;
        if let Some(supervisor) = supervisor {
            supervisor.stop().await;
        }
        monitor.stop().await;
        proxy.stop().await;
        result?;
        Ok(())
    });
    rt.shutdown_timeout(std::time::Duration::from_secs(2));
    result
}
