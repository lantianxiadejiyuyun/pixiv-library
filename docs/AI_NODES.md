# 远程 AI 控制器

主服务器负责书库、任务队列和结果入库；另一台电脑运行配套控制器、Ollama 与图像识别容器。无需把另一台电脑的整个 Docker API 开放给书库。Web「AI 整理 → 远程 AI 控制器」提供部署包下载、节点管理、容器启停、模型管理和执行位置切换。

## 在 AI 电脑部署

安装 Docker Engine 或 Docker Desktop（Linux 容器模式），并准备 Python 3.10 以上。下载节点部署包并解压，在解压后的目录运行：

```console
python scripts/bootstrap_ai_node.py
python scripts/ai_node.py up -d --build
```

第一条命令新建私有 `deploy/ai-node.env`，生成节点 ID 和随机密钥；不会覆盖已有配置，也不在终端打印密钥。第二条命令启动独立的 `qiye-ai-node` 项目，不启动书库或 MySQL。

控制器默认监听 9010 端口。将 AI 电脑的局域网地址（例如 `http://192.168.1.20:9010`）填到主服务器的节点页面，密钥填入 `deploy/ai-node.env` 中 `QIYE_CONTROLLER_KEY` 的值。保存后点击「连接并刷新」。其他电脑访问不到时，检查该电脑防火墙是否允许来自主服务器的 9010/TCP。

容器仅在内部网络暴露 Ollama 11434 与图像服务 9011，不需要额外映射这两个端口。主服务器只访问控制器。节点请求不使用主服务器的 Clash 出口，模型下载使用 AI 电脑自己的网络。

控制器挂载该 AI 电脑的 Docker socket，具有 Docker 主机权限；只在可信局域网或 VPN 中使用并保管密钥。公网连接须自行配置 HTTPS 反向代理，主服务器不接受公网明文 HTTP 节点地址。控制器接口仅允许管理带有匹配节点及服务标签的 Ollama／图像容器，不提供任意命令执行、挂载、容器创建或宿主机文件读取接口。

## Ollama 模型

在节点页面输入模型名称与标签，加入模型下载队列。模型拉取在 AI 电脑后台执行，页面显示阶段、当前层字节进度和结果；离开页面不取消任务。再次连接节点可读取最近任务。相同模型的未完成下载会复用任务，每个节点同时下载一个模型，最多等待八项。

模型目录显示磁盘中的模型及内存中已加载的模型，可释放模型内存、删除已安装模型。点击「用于标题与标签汉化」会明确启用该节点的 Ollama 模型；也可以在书库设置的汉化配置中选择「远程 AI 控制器」。只发送标题与标签，原文保留。

模型拉取不自动选择或下载其他模型。控制器重启后未完成任务标记为失败，可重新提交，Ollama 可复用已经下载的数据。当前层完成字节数不代表整个模型总下载进度。模型下载记录保存在独立 `controller_state` 卷，模型权重保存在 `ollama_models` 卷。

## 图像标签、角色及 R18 分级

将同一 WD Tagger 版本的两个文件放在 AI 电脑的模型目录：

```text
models/
  wd-tagger/
    model.onnx
    selected_tags.csv
```

模型文件需自行取得，控制器不会自动下载图像权重。可在 `deploy/ai-node.env` 设置 `QIYE_AI_MODEL_PATH` 指向 NAS 挂载目录。Windows Docker Desktop 需允许共享该路径，Linux/NAS 需确保容器可以读取。模型目录只读挂载。

连接节点后，图像状态区显示模型文件及运行时是否就绪；首次实际分析才验证模型与设备兼容性。在「图片识别执行位置」选择该节点并保存，后续 AI 识别使用远程节点。图片上传至节点临时目录，单张传输上限 16 MiB；更大的原图先缩放并重新编码再发送。请求结束后删除临时文件，不把原始书库复制到 AI 电脑。

全部页面分析成功且执行节点配置保持一致后，主服务器才保存标签和分级建议。连接中断、错误结果、节点变更或任务取消都保留已有结果，不自动切换到其他电脑或本机。人工标签、人工分级，以及来源已经标记的 R18 内容继续按原规则保护。纯文字关键词分类仍在主服务器完成。

图像服务同时执行一个请求，Ollama 聊天入口最多两个并发请求。可在主服务器的 AI／汉化队列设置相应并发；超过节点容量时返回繁忙，不会启动无限推理进程。将图片识别执行位置切回「主服务器本机」后，恢复原本的本地推理流程。

## GPU 与 CPU

默认图像服务使用 ONNX Runtime CPU。Intel 核显电脑可用：

```console
python scripts/ai_node.py --intel up -d --build
```

Intel 覆盖会构建带 OpenVINO 和 Intel OpenCL 用户态驱动的 amd64 镜像，并将 `/dev/dri` 传给图像服务；需要主机已有兼容驱动。实际 GPU 不可用或图像模型不兼容时使用现有 CPU 回退，并在执行证据中报告实际设备。

需要让 Ollama 使用 NVIDIA 显卡时，在支持 NVIDIA 容器运行时的电脑执行：

```console
python scripts/ai_node.py --nvidia up -d --build
```

此覆盖只给 Ollama 分配 NVIDIA 设备，图像服务仍使用 CPU。容器不能替主机安装显卡驱动。不同 GPU、模型和 NAS 组合需在实际设备验证。

## 容器管理与维护

节点页面可启动、停止或重启 Ollama／图像服务。操作只作用于当前节点标签对应的容器；不会控制书库、MySQL 或其他 Docker 项目。停止或重启服务可能使正在处理的任务失败，之后可从主服务器任务页重试。

若显示「尚未部署」，先在 AI 电脑执行 Compose；页面不接受任意 Docker 镜像或容器启动参数。需要修改端口、模型路径、GPU 或镜像版本时，编辑 AI 电脑上的 `deploy/ai-node.env`／Compose 文件后重新运行部署命令。Ollama 镜像默认固定为 `ollama/ollama:0.40.5`，可通过 `OLLAMA_IMAGE` 明确更换。

移除节点连接不会删除远程容器、模型或卷。正在被图片识别或汉化配置引用的节点需先切换执行位置，再移除连接。停用或断开当前节点时不自动转发到其他模型。节点密钥在主服务器数据库中加密保存，读取配置时不会返回；更换地址时必须重新输入密钥。

接口依据：[Docker Engine API](https://docs.docker.com/reference/api/engine/)、[Ollama 模型目录](https://docs.ollama.com/api/tags)、[Ollama 模型拉取](https://docs.ollama.com/api/pull)、[Ollama 聊天 API](https://docs.ollama.com/api/chat)。
