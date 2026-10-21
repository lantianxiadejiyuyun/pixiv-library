//! Only the independent model/controller deployment sources are packaged.
//! The native server never invokes these Python files as backend adapters.
use crate::{
    AppState,
    error::{ApiError, Result},
    item_api,
};
use axum::{extract::State, response::Response};
use std::{
    io::Write,
    path::{Path, PathBuf},
};
fn allowed(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|p| p.to_str()) else {
        return false;
    };
    let parent = path
        .parent()
        .and_then(|p| p.to_str())
        .unwrap_or("")
        .replace('\\', "/");
    match parent.as_str() {
        "server/ai_controller" | "server/app" | "server/app/ai" | "server/app/integrations" => {
            name.ends_with(".py")
        }
        "server" => {
            name == "requirements-controller.txt"
                || (name.starts_with("requirements-ai") && name.ends_with(".txt"))
        }
        "deploy" => {
            (name.starts_with("compose.ai-node") && name.ends_with(".yaml"))
                || name.starts_with("Dockerfile.ai-controller")
                || name == "ai-node.env.example"
        }
        "scripts" => [
            "ai_node.py",
            "bootstrap_ai_node.py",
            "install_intel_opencl.py",
        ]
        .contains(&name),
        "docs" => name == "AI_NODES.md",
        _ => false,
    }
}
pub async fn get(State(s): State<AppState>) -> Result<Response> {
    let permit = s
        .export_limit
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError::bad("已有文件导出正在进行"))?;
    let root = std::env::var_os("LIBRARY_AI_BUNDLE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."));
    let file = tokio::task::spawn_blocking(move || -> Result<_> {
        let mut paths = vec![];
        for folder in [
            "server/ai_controller",
            "server/app",
            "server/app/ai",
            "server/app/integrations",
            "server",
            "deploy",
            "scripts",
            "docs",
        ] {
            let dir = root.join(folder);
            if !dir.is_dir() {
                continue;
            }
            for entry in std::fs::read_dir(dir)?.take(1000) {
                let entry = entry?;
                let relative = PathBuf::from(folder).join(entry.file_name());
                if allowed(&relative)
                    && entry.file_type()?.is_file()
                    && !entry.file_type()?.is_symlink()
                {
                    paths.push(relative);
                }
            }
        }
        if !paths
            .iter()
            .any(|p| p == Path::new("deploy/compose.ai-node.yaml"))
        {
            return Err(ApiError::missing("镜像缺少独立 AI 节点部署文件"));
        }
        paths.sort();
        paths.dedup();
        let mut file = tempfile::NamedTempFile::new()?;
        let mut total = 0;
        {
            let mut zip = zip::ZipWriter::new(file.as_file_mut());
            let options = zip::write::FileOptions::default()
                .compression_method(zip::CompressionMethod::Deflated)
                .unix_permissions(0o644);
            for path in paths {
                let full = root.join(&path);
                let size = full.metadata()?.len();
                total += size;
                if size > 4 * 1024 * 1024 || total > 32 * 1024 * 1024 {
                    return Err(ApiError::bad("AI 部署包超过大小限制"));
                }
                let name = path.to_string_lossy().replace('\\', "/");
                zip.start_file(&name, options)
                    .map_err(|_| ApiError::bad("AI 部署包写入失败"))?;
                std::io::copy(&mut std::fs::File::open(&full)?, &mut zip)?;
                if name == "docs/AI_NODES.md" {
                    zip.start_file("README.md", options)
                        .map_err(|_| ApiError::bad("AI 部署包写入失败"))?;
                    std::io::copy(&mut std::fs::File::open(full)?, &mut zip)?;
                }
            }
            zip.start_file(".gitignore", options)
                .map_err(|_| ApiError::bad("AI 部署包写入失败"))?;
            zip.write_all(b"deploy/ai-node.env\nmodels/\n**/__pycache__/\n")?;
            zip.finish()
                .map_err(|_| ApiError::bad("AI 部署包写入失败"))?;
        }
        file.flush()?;
        Ok(file)
    })
    .await
    .map_err(|_| ApiError::bad("AI 部署包生成失败"))??;
    item_api::temporary_owned(
        file,
        "qiye-ai-node-0.1.0.zip",
        "application/zip",
        Some(permit),
    )
    .await
}
