# Rust 后端

主服务和采集队列的原生 Rust 实现，位于 `server/rust/`。152 个旧 API 路由均已接入，保持 Web 和 Flutter 使用的路径及数据结构。程序不启动 Python、不调用旧后端填补业务。原 Python 实现保留用于对照和回退，现有线上部署未切换。

AI 推理保持独立：使用原有图像服务／AI 控制器和 Ollama／兼容接口。Docker 镜像中的 Python 源文件仅作为可下载的独立 AI 部署包，不由主服务执行。

## 功能

- MySQL 异步连接池、现有数据表与加密凭据兼容；SQLite 仅用于开发和测试。
- Pixiv、E-Hentai、PicACG、JM、Kemono 的原生协议实现、账号登录、下载、元数据与媒体保存。Pixiv 包括 OAuth、插画分 P、漫画／小说系列、小说插图、作者主页、关注、公开收藏和追更。
- 8 个独立工作进程：手动下载、重试、自动下载、详情、作者／订阅检查、AI、汉化、其他。任务使用数据库租约与持有者校验；异常退出可恢复。
- 主账号与副账号分工、每账号并发及各队列总并发、重试权限交集、独立暂停原因、平台冷却及随机请求间隔。手动和重试不受统一采集开关控制。
- 书库、作者目录、标签多选、收藏分组、评分、历史、系列、阅读进度、视频 Range／HEAD、小说章节／内嵌插图、手机管理和扫码绑定。
- NAS 安全路径、内容寻址文件、完整性校验、缩略图缓存、ZIP／CBZ 导入导出、MySQL 一致性导出、APK 签名校验和发布、服务器测速与性能面板。
- 内置 Mihomo 订阅、节点选择、定时更新与批量测速。只接受节点配置，不执行远程规则、脚本或本地文件引用；失效时阻断，不回退直连。
- 人物作品墙、AniList 资料候选、显式单图 SauceNAO 搜索。联网图搜需要明确确认，上传前缩图并移除元数据。

`GET /api/rust/migration` 返回已接入与剩余路由、只读状态及 `workers_migrated=true`。`production_ready=false` 表示尚未完成真实账号与生产副本切换验收，不能据路由数量推断所有上游协议已实测可用。

## 构建与启动

建议 Rust 1.90；已验证 Linux amd64。Windows 编译需可用的本机 Rust C 工具链；本次 Windows 环境的构建脚本受到系统应用控制限制，因此编译和进程测试在 Docker Linux 中完成。未验证 ARM64 和 Windows 原生发布包。

```sh
cargo build --locked --release --manifest-path server/rust/Cargo.toml
```

配置沿用 `LIBRARY_DATABASE_URL`、`LIBRARY_STORAGE_ROOT`、`LIBRARY_IMPORT_ROOT`、`LIBRARY_ADMIN_PASSWORD`、`LIBRARY_SECRET_KEY`、`LIBRARY_ALLOWED_ORIGINS`、`LIBRARY_SECURE_COOKIE`、`LIBRARY_FRONTEND_DIST`、`LIBRARY_ENV`。兼容 `mysql+pymysql://` 格式，原库字符集不变。生产模式要求 MySQL、8–20 位非默认密码及至少 32 字符的密钥。

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `LIBRARY_RUST_BIND` | `127.0.0.1:8801` | 独立监听地址 |
| `LIBRARY_RUST_READ_ONLY` | `true` | 阻止业务写入，关闭后台采集 |
| `LIBRARY_RUST_WORKERS_ENABLED` | `false` | 显式启用 8 个 Rust 队列进程 |
| `LIBRARY_RUST_DB_CONNECTIONS` | `12` | API 连接池，1–64；每工作进程另用 4 个连接 |
| `LIBRARY_CPU_AFFINITY` | `auto` | 自动分配 CPU；也可 `off` 或队列 CPU 编号 JSON |
| `LIBRARY_AI_VISION_URL` | 空 | 独立图像服务／控制器地址 |
| `LIBRARY_AI_VISION_KEY` | 空 | 该服务的密钥 |
| `LIBRARY_AI_BUNDLE_DIR` | 源码仓库根目录 | 独立 AI 部署包的来源目录 |
| `LIBRARY_MIHOMO_PATH` | `.runtime/mihomo/mihomo` | 已安装的 v1.19.32 内核；Windows 加 `.exe` |
| `LIBRARY_MIHOMO_PROXY_PORT` | `17890` | 内核回环代理端口 |
| `LIBRARY_MIHOMO_CONTROLLER_PORT` | `19090` | 内核回环控制端口 |

AI 节点和标题／标签汉化也可在现有 Web／App 设置中管理。图像服务沿用独立节点的 CPU／Intel 等执行配置，Rust 主服务不装载模型。没有可用模型时明确报告未配置。

自动 CPU 分配会给 API 保留核心，再分配给队列；核心不足时会共享并在性能面板显示。CPU 亲和性不能保证独占物理核心。所有进程继续遵守队列和账号并发上限。

### 数据库与 NAS

空数据库显式初始化：

```sh
server/rust/target/release/qiye-server migrate
server/rust/target/release/qiye-server
```

初始化需要 `LIBRARY_RUST_READ_ONLY=false`。正常启动只校验结构，不自动建表。已有数据库必须是原后端版本 `e71b4c8213de`；未知版本、缺少版本标记或表结构不完整时拒绝启动，不重置数据。

沿用原来的 `LIBRARY_SECRET_KEY`，否则现有账号与设置无法解密。媒体继续使用现有 NAS 目录，各平台独立命名空间。不在启动时扫描全部媒体；标记或 NAS 不可用时降级文件服务，数据库查询仍可用。生产实例不会自行创建 `.library-storage`，避免向错误挂载点写入。

### Docker

```sh
docker build -f deploy/Dockerfile.rust -t qiye-library-rust:0.1.0 .
docker compose --project-directory . --env-file deploy/.env -f deploy/compose.rust.yaml --profile initialize run --rm migrate
docker compose --project-directory . --env-file deploy/.env -f deploy/compose.rust.yaml up -d app
```

Compose 默认另用项目 `qiye-library-rust`、独立 MySQL 卷和主机端口 8801；应用保持只读、队列关闭。验证完数据库副本后，再明确设置 `LIBRARY_RUST_READ_ONLY=false` 和 `LIBRARY_RUST_WORKERS_ENABLED=true`。不要让两套采集进程同时写入同一个库。

镜像包括 APK 校验工具和校验过下载摘要的 Mihomo v1.19.32。Mihomo 使用 GPL-3.0，许可证位于镜像 `/usr/share/doc/mihomo/LICENSE`，源码及发布文件见 [MetaCubeX/mihomo](https://github.com/MetaCubeX/mihomo/tree/v1.19.32)。运行时不自动下载内核。独立 AI 计算节点仍使用 `deploy/compose.ai-node.yaml`，无需给主服务开放 Docker socket。

## 验证

```sh
cargo test --locked --manifest-path server/rust/Cargo.toml
cargo clippy --locked --manifest-path server/rust/Cargo.toml --all-targets -- -D warnings
cargo fmt --manifest-path server/rust/Cargo.toml --check
```

包括 38 份旧 API 响应对照、账号与设备权限、事务与租约、导入完整性、小说插图、文件 Range、人物墙、模拟图像服务／Ollama 的完整任务、配置变化失效，以及 Linux 下真实二进制的八队列启动、CPU 亲和性、杀死队列后的恢复和父进程退出测试。协议用例检查不完整分 P、JM 图片还原、媒体完整性、订阅过滤、APK 元信息及公开人物结果。

`contracts/` 只有合成数据；`tools/snapshot_contract.py` 在临时 SQLite 中生成旧接口对照，不连接业务数据库。更新夹具后需要检查差异。

MySQL 用例须显式使用一次性 Docker 库：主机名 `qiye-rust-mysql`、库名 `qiye_rust_contract`、用户名 `contract`，通过 `QIYE_RUST_TEST_MYSQL_URL` 传入。用例首先拒绝非空库，之后会在这个合成库内执行导出、删除测试表和恢复，以验证生成列、JSON、中文与数值完整性。失败后重建专用容器；不要使用现有业务库。

```sh
cargo test --locked --test mysql_compatibility -- --ignored
LIBRARY_MIHOMO_PATH=/path/to/mihomo cargo test --locked --lib pinned_core -- --ignored
```

Mihomo 测试只验证本机控制器、指定节点和失效阻断，不发送外部流量。本次未使用真实平台账号重新登录或下载，未做生产规模压力测试，也未更新 220 服务器。正式切换仍需使用数据副本验证真实平台和实际 NAS；保留原镜像与数据库备份用于回退。

完整镜像冒烟：`python server/rust/tools/smoke_image.py --image qiye-library:rust-validation`。脚本只创建新的临时目录、数据库和专用容器，验证页面、AI 部署包、只读保护、8 个队列、性能面板、测速和 NAS 降级，结束后清理这些测试资源。
