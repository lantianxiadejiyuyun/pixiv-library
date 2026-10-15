# 本地模型

运行时不下载模型、不调用云端推理。图片识别与文章规则分类独立。

## WD Tagger

1. 安装 `pip install -r server/requirements-ai.txt`，或构建 Docker 时设置 `INSTALL_AI=true`。
2. 从 [SmilingWolf/wd-swinv2-tagger-v3](https://huggingface.co/SmilingWolf/wd-swinv2-tagger-v3) 手动取得同一版本的 `model.onnx` 和 `selected_tags.csv`。遵守模型页面的使用条件。
3. 放在 `LIBRARY_MODEL_DIR/wd-tagger/model.onnx` 与 `LIBRARY_MODEL_DIR/wd-tagger/selected_tags.csv`。
4. 在 AI 页面确认文件可见，再对一个小作品进行分析。第一次任务会验证 ONNX 输入与标签输出是否匹配。

预处理按照 WD 系列模型约定：透明区域铺白、正方形填充、缩放到输入尺寸、RGB 转 BGR、NHWC float32、0–255 像素。仅支持该格式模型；不能任意替换为其他 ONNX 文件。

`LIBRARY_AI_PROVIDER=auto` 按已安装的运行时尝试 CUDA、Intel OpenVINO GPU、CPU；`intel` 尝试 Intel GPU 后回退 CPU；`cpu` 强制 CPU；`cuda` 保持严格模式，不可用时明确报错。自动／Intel 模式的加速初始化、编译、推理异常和原生驱动进程退出都会回退 CPU，重试当前输入。CPU 也失败则任务失败，不制造结果。

Intel 依赖使用 `requirements-ai-intel.txt`，应替代默认 `requirements-ai.txt`，不能混装多个 ONNX Runtime 发行包。固定兼容组合为 ORT OpenVINO 1.24.1／OpenVINO 2025.4.1。Intel 子进程通过设备名称筛选 Intel GPU，禁止将其他厂商 OpenCL GPU 当作 Intel；设备调用隔离在子进程，默认最多等待 120 秒。具体 Windows、Linux/NAS 启动方式见根 README。

API 顶层 `requested_provider` 表示配置，`actual_provider`、`device` 只在成功推理后填入；`execution_state` 为 `not_loaded/ready/fallback/error`，`fallback_reason` 提供安全说明。每页结果同时保存 `execution`。仅运行时可见不代表 GPU 已通过模型验证；在没有 Intel 硬件的本机只验证了真实 CPU 推理与 Intel→CPU 回退。

通用标签阈值 0.35，角色标签阈值 0.85。分级保留每页完整分数；最高分低于 0.6 的页面标记为未知。汇总时明确的成人页优先，其次是未知页，因此任意不确定页都会阻止整本被判为普通。阈值是初始默认值，尚未用用户数据集校准。仅分析完成全部图片后才建议分级；任意页面出错则整个任务失败、保留原数据。

角色来自模型已有词表，并非自建角色脸库。页面分级是模型建议，仍需人工复核。人工设置的标签／分级不会被重跑覆盖，来源明确标为成人的作品不会被 AI 自动降级。旧 AI 标签仅在该作品的新分析全部成功后替换。每次结果记录权重与标签文件内容组合哈希作为版本、页级分数和最强证据页。

## 文章分类

`text-rules-v1` 是本地可解释关键词规则（科技、文学、历史、生活等），不是训练模型。任务结果保存命中词；无命中时明确显示“未命中分类规则”，不捏造类别。规则位于 `service.py`。

OCR、自建角色库、语义向量、多个 GPU 的任务调度及独立局域网 AI 节点尚未实现。当前部署支持同机本地推理，不应将本版本描述为已支持上述能力。

## 手动联网查证

AniList 文字检索与 SauceNAO 图搜由独立 API 提供，不在本地模型／后台整理任务内自动调用。文字只发送用户确认的查询词；图搜只在用户明确同意并提交后发送选中的一张图片，经限大小、缩放和去元数据处理。返回的是待核对候选，相似度不是身份置信度。人工确认接口原子追加 `角色:` 标签，不覆盖已有人工、来源或 AI 标签。

## 源适配参考

- [PixivPy API](https://github.com/upbit/pixivpy)：刷新令牌登录、插画／小说、作者关注列表。
- [E-Hentai gdata](https://ehwiki.org/wiki/API)：元数据与分页画廊下载；图片来自每页 `img#img` 当前展示清晰度，完整页数校验后才入库。没有调用消耗 GP 的原图或归档接口。参照 [站点下载说明](https://ehwiki.org/wiki/Downloading)。
- [JMComic Python](https://github.com/hect0x7/JMComic-Crawler-Python)：固定站点元数据与本地图片还原，未启用第三方插件。
- [PicACG API wrapper](https://github.com/l2studio/picacomic-api)：登录、作品／章节／图片、收藏导入和标签搜索；作者关注未实现。

所有平台适配器需要用户自己的有效账号与可达网络验证；测试只使用协议级模拟数据，没有替用户访问真实作品。关注、收藏同步只读取远端，不改变远端账号关注关系。定时订阅目前检查最新一页，历史补全尚未实现。
