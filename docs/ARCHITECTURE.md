# 架构与数据约定

## 模块

- `web/`：Vue 3 / TypeScript SPA；构建结果由后端同源提供。
- `app/`：独立 Flutter Android / iOS 手机应用，扫码绑定服务器后使用受限设备凭据。
- `server/app/main.py`：登录、书库、导入、阅读资源、导出、管理接口。
- `server/app/library.py`：输入校验、内容建档、标签及作品视图。
- `server/app/storage.py`：不可变内容寻址文件，读写前检查存储标记。
- `server/app/integrations/`：平台能力、凭据和远端内容适配。
- `server/app/source_auth*.py`：账号密码登录验证与 Pixiv 网页授权接口；平台成功后才保存会话。
- `server/app/device_auth.py`、`device_api.py`：一次性扫码绑定、设备凭据哈希、阅读权限白名单和撤销。
- `server/app/network*.py`：加密代理配置、统一出口、Clash 控制接口和平台连接检测。
- `server/app/managed_proxy*.py`、`proxy_subscription.py`、`managed_core.py`：订阅解析、加密库存、自动更新及内置 Mihomo 子进程。
- `server/app/collections_api.py`、`collection_migration.py`：分平台统计、旧文件的校验复制与路径迁移。
- `server/app/jobs.py`：持久任务、租约领取、订阅检查和工作进程。
- `server/app/ai/`：本地模型加载、页级证据和全书汇总。
- `server/migrations/`：MySQL/SQLite 兼容的版本迁移。

首版单管理员、单应用进程。数据库存储元数据和任务，文件系统存储媒体。独立 AI 节点、多租户和复杂作者实体不是本版已完成能力。

源码目录调整不改变 Python 包名 `app`、Compose 服务名 `app` 或数据库／媒体目录。容器内后端为 `/app/server`，网页构建产物为 `/app/web/dist`；Flutter 应用独立编译，不包含在服务端镜像中。

部署文件集中在 `deploy/`，项目说明和开发合同在 `docs/README.md`、`docs/CONTRACT.md`，pytest 配置在 `server/pytest.ini`。`scripts/compose.py` 与 PowerShell／Shell 包装器固定根项目目录、环境文件及 Compose 文件，避免迁移配置后意外换用数据路径或卷。`deploy/Dockerfile.dockerignore` 作用于根构建上下文，排除凭据、运行目录、媒体和 Flutter 工程。

Git 忽略规则／文件属性移至 `config/git/ignore`、`config/git/attributes`，通过 `scripts/configure_git.py` 配置当前 checkout 的绝对路径。自身有 `.git` 时，初始化脚本先完成 Git 配置再创建凭据；ZIP／无 Git 的 NAS 仍可正常初始化。`deploy/.gitignore` 额外保护凭据，新克隆、整体移动仓库或日后纳入 Git 时应运行配置脚本。

## 文件事务

原文件通过 SHA-256 路径存放，名称与作品关系存在数据库。先在目标文件系统写临时文件、flush/fsync、读回校验和重命名，再提交数据库关系。失败可能留下无引用文件，但不会让未完成写入显示成可读作品。删除条目仅移除库内关系，不自动删共享原文件；缩略图可重建。

来源命名空间为 `collections/{pixiv,ehentai,picacg,jm,local}/blobs/aa/bb/hash`。同来源去重，跨来源分别存放。迁移逐作品锁定、验证和复制全部资源后再提交引用；保留旧 `blobs`，读接口兼容旧路径，备份覆盖两个目录体系。集合统计由 MySQL 的真实作品与资源聚合，内容字节总数并非磁盘占用量。

NAS 掉线时标记缺失阻止继续读写。应用启动检查引用文件是否存在及长度是否匹配，不自动删除失效条目。完整 SHA-256 复核由备份校验或专用检查完成；实际 SMB/NFS 行为仍须在目标设备上验证。

## 任务

任务持久保存，领取使用条件更新和租约。执行至少一次，因此远端身份和文件哈希负责幂等。取消是协作式的：网络读取或单页推理要到检查点才停止；最终写入必须确认仍拥有运行任务。界面保留失败原因和可重试入口。

订阅规则和平台账号关联。第一版的定时检查范围以适配器能力为准，历史补全不应被一次最新页检查冒充。暂不支持的平台规则保存为待实现状态。

## 标注

标签关联包含 `manual`、`source`、`ai` 来源，AI 结果保存模型版本、分数与证据。人工分级拥有覆盖权，模型重跑不会重置它。漫画只有全部目标页完成检测后才提交全书结果；文本分类规则不会把无图片作品判定成普通级别。

当前分类树以 `分类:` 标签表达、角色候选以 `角色:` 标签表达；完整角色实体和参考图库后续迁移。

## 网络与权限

会话 cookie 为 HttpOnly、SameSite Strict；通过源站校验保护写操作。生产反向代理使用 HTTPS 时设置 `LIBRARY_SECURE_COOKIE=true`，将真实浏览器 origin 加入允许列表。远端链接按平台域名验证，手动导入只能读取配置的导入根。文本作为纯文本返回，不执行来源 HTML。

`device_pairings` 使用单个授权槽存储二维码 token 的 SHA-256 摘要，5 分钟过期，条件更新与设备创建在同一事务内完成，防止重复兑换。`device_sessions` 仅存设备 token 摘要、名称、使用时间和撤销／到期时间；设备有效期为 90 天。设备 Bearer 请求在统一路由白名单中校验，只能阅读、保存进度、请求正文下载及自行解绑，即使携带管理员 cookie 也不能取得管理权限。

应用端口可公开给局域网，MySQL 不发布宿主机端口。首次发布到公网前应配置 HTTPS 和入口访问策略。平台账号、会话密钥和数据库密码不出现在 API 返回或日志中。

`network_settings` 保存加密配置；每个新平台客户端读取一次，不修改操作系统代理或进程环境。HTTPX/PixivPy/JM 的实际请求路径都显式传入代理，关闭时明确直连。Clash 控制请求独立直连，只允许固定 `/proxies` 查询和编码后的策略组选择，不代理任意控制命令。控制器 URL 是管理员明确配置的局域网/回环服务，禁止跳转，以免密钥发送到其他主机。

内置代理优先于外部代理。订阅 HTTPS 下载后仅保留允许的节点字段，不执行订阅中的规则、providers、监听器或本地文件引用。内核先验证候选配置，应用成功后提交数据库；数据库失败回滚内核，无法回滚则停机。当前节点消失保持 REJECT，不自动选择其他出口。持久数据使用加密 MEDIUMTEXT，节点 ID 由完整订阅 ID 与节点名派生。

Mihomo 为固定校验版本，单独子进程随生命周期启停，控制器带随机密钥且只绑定回环。运行目录权限受限；自动监管在 5 分钟最多重启 3 次，回滚失败禁用重启。自动订阅更新与用户修改串行；关闭服务与正在提交的更新同步，避免关闭后重新拉起进程。运行时不下载二进制，不要求特权或 TUN。

启用代理时由代理执行目标 DNS，下载仍按平台 HTTPS 域名白名单检查每次跳转。已有连接使用创建时配置；配置改变影响后续连接。失败没有自动直连回退。高级外部 Clash 节点切换可能影响其共用客户端；程序不改外部实例规则。

`SourceAccount.verified_at` 记录首次会话验证成功时间，空值代表手工导入的未验证凭据，不代表登录失效检查。Pixiv PKCE verifier 保留在服务器内存十分钟，绑定本站 cookie 的摘要，一次性消费，重启即失效。授权码需从官方网页回调手动带回；接口不是 Pixiv 为任意第三方注册的官方 OAuth 应用。
