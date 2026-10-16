"""Native mobile management forms, derived from the existing validated contracts.

This is a description endpoint, not a second execution API. Every action still
uses its original route, authorization, validation and queue implementation.
"""
import copy

from fastapi import APIRouter, Depends, Request

from .auth import require_admin_cookie

router = APIRouter(dependencies=[Depends(require_admin_cookie)])

GROUPS = {
    "overview": "运行概况", "sources": "平台与账号", "queue": "任务与队列",
    "subscriptions": "内容订阅", "network": "代理与节点", "ai": "AI 与汉化",
    "library": "作品与书库", "devices": "设备与服务器",
}

TITLES = {
    ("GET", "/health"): "服务健康", ("GET", "/dashboard"): "书库概况",
    ("GET", "/settings"): "服务器设置", ("GET", "/sources"): "平台与账号",
    ("POST", "/sources/{platform}/accounts"): "导入账号凭据",
    ("DELETE", "/sources/{platform}/accounts/{account_id}"): "移除平台账号",
    ("POST", "/sources/{platform}/login"): "登录平台账号",
    ("POST", "/sources/pixiv/oauth/start"): "开始 Pixiv 授权",
    ("POST", "/sources/pixiv/oauth/finish"): "完成 Pixiv 授权",
    ("GET", "/sources/pixiv/account-roles"): "查看 Pixiv 账号分工",
    ("PUT", "/sources/pixiv/account-roles"): "保存 Pixiv 账号分工",
    ("POST", "/sources/{platform}/import"): "导入平台作品",
    ("POST", "/sources/{platform}/sync"): "同步关注内容",
    ("GET", "/platform-endpoints"): "查看平台接口地址",
    ("PUT", "/platform-endpoints/{platform}"): "修改平台接口地址",
    ("GET", "/jobs"): "任务列表", ("GET", "/jobs/{job_id}"): "任务详情",
    ("POST", "/jobs/{job_id}/retry"): "重试任务",
    ("POST", "/jobs/{job_id}/cancel"): "取消任务",
    ("GET", "/queue/settings"): "查看采集设置",
    ("PUT", "/queue/settings"): "修改采集设置",
    ("GET", "/queue/status"): "队列运行状态",
    ("PUT", "/queue/lanes/{lane}"): "设置独立队列",
    ("POST", "/queue/covers"): "补全缺失封面",
    ("GET", "/subscriptions"): "订阅列表", ("POST", "/subscriptions"): "添加内容订阅",
    ("PATCH", "/subscriptions/{subscription_id}"): "修改内容订阅",
    ("DELETE", "/subscriptions/{subscription_id}"): "删除内容订阅",
    ("GET", "/network/proxy"): "查看代理配置", ("PUT", "/network/proxy"): "修改代理配置",
    ("POST", "/network/proxy/test"): "测试平台连通性",
    ("GET", "/network/clash/groups"): "Clash 策略组",
    ("POST", "/network/clash/select"): "选择 Clash 节点",
    ("GET", "/network/managed"): "内置代理与节点",
    ("POST", "/network/managed/subscriptions"): "导入代理订阅",
    ("PATCH", "/network/managed/subscriptions/{subscription_id}"): "修改代理订阅",
    ("POST", "/network/managed/subscriptions/{subscription_id}/refresh"): "更新代理订阅",
    ("DELETE", "/network/managed/subscriptions/{subscription_id}"): "删除代理订阅",
    ("POST", "/network/managed/enable"): "启用或关闭内置代理",
    ("POST", "/network/managed/select"): "选择代理节点",
    ("POST", "/network/managed/delay"): "测试单个节点",
    ("POST", "/network/managed/delay-batch"): "一键节点测速",
    ("GET", "/network/managed/delay-batch"): "最近测速结果",
    ("GET", "/network/managed/delay-batch/{batch_id}"): "查看测速批次",
    ("POST", "/network/managed/restart"): "重启内置代理",
    ("GET", "/ai/status"): "本地 AI 状态", ("POST", "/ai/analyze"): "提交本地 AI 识别",
    ("GET", "/ai/characters/image/settings"): "查看图搜设置",
    ("PUT", "/ai/characters/image/settings"): "修改图搜设置",
    ("POST", "/ai/characters/image-search"): "以图搜索人物",
    ("POST", "/ai/characters/search"): "联网搜索人物",
    ("POST", "/ai/characters/confirm"): "确认人物标签",
    ("GET", "/translation/status"): "汉化运行状态",
    ("GET", "/translation/settings"): "查看汉化模型",
    ("PUT", "/translation/settings"): "修改汉化模型",
    ("POST", "/translation/test"): "测试汉化模型",
    ("POST", "/translation/batch"): "批量汉化标题与标签",
    ("GET", "/items"): "管理书库作品", ("GET", "/items/{item_id}"): "查看作品详情",
    ("PATCH", "/items/{item_id}"): "编辑作品资料",
    ("DELETE", "/items/{item_id}"): "移除作品记录",
    ("POST", "/items/{item_id}/download"): "下载作品正文",
    ("POST", "/items/{item_id}/cover-refresh"): "重新获取封面",
    ("POST", "/items/{item_id}/translate"): "汉化单个作品",
    ("GET", "/items/{item_id}/translation"): "作品汉化进度",
    ("GET", "/items/{item_id}/export"): "导出作品文件",
    ("POST", "/imports/upload"): "从手机上传作品",
    ("POST", "/imports/folder"): "导入服务器目录",
    ("GET", "/tags"): "标签列表", ("GET", "/library/collections"): "平台书库统计",
    ("POST", "/series/import"): "导入 Pixiv 官方系列",
    ("POST", "/series"): "创建手动系列", ("GET", "/series/{series_id}"): "查看系列",
    ("PUT", "/series/{series_id}"): "编辑系列与上下集",
    ("GET", "/devices"): "已绑定设备", ("DELETE", "/devices/{device_id}"): "撤销设备授权",
    ("POST", "/devices/pairing"): "生成手机绑定码",
    ("POST", "/database/export"): "导出 MySQL 数据库",
    ("GET", "/app-updates/android"): "查看 Android 更新",
    ("POST", "/app-updates/android"): "发布 Android 安装包",
    ("GET", "/favorite-groups"): "查看收藏分组", ("POST", "/favorite-groups"): "创建收藏分组",
    ("PUT", "/favorite-groups/{group_id}"): "修改收藏分组", ("DELETE", "/favorite-groups/{group_id}"): "删除收藏分组",
    ("DELETE", "/history"): "清空浏览历史",
    ("GET", "/characters"): "人物作品墙", ("GET", "/characters/{tag_id}"): "人物资料",
    ("POST", "/characters"): "创建人物资料", ("POST", "/characters/search"): "搜索人物资料",
    ("POST", "/characters/{tag_id}/profile"): "更新人物介绍",
    ("GET", "/pixiv/authors"): "作者列表", ("POST", "/pixiv/authors/open"): "读取作者主页",
    ("GET", "/pixiv/authors/{author_id}"): "作者资料",
    ("GET", "/pixiv/authors/{author_id}/feed"): "作者作品与收藏",
    ("POST", "/pixiv/authors/{author_id}/feed"): "获取作者目录",
    ("GET", "/pixiv/tasks/{job_id}"): "作者采集进度",
}

# Display labels only; schemas and validators continue to come from each route.
LABELS = dict(part.split("=", 1) for part in """
platform=平台|account_id=平台账号|main_account_id=检索主账号|content_account_id=图片下载账号|content_account_ids=图片下载账号|detail_account_ids=作品详情与正文账号|check_account_ids=作者资料账号|account_limits=每账号并发|enabled=启用|paused=暂停|concurrency=并发数|rate_limit_enabled=启用请求限流|min_interval_seconds=最小请求间隔（秒）|max_interval_seconds=最大请求间隔（秒）|request_interval_seconds=请求间隔（秒）|lane=队列|job_id=任务|item_id=作品|item_ids=作品|subscription_id=订阅|device_id=设备|series_id=系列|tag_id=人物标签|batch_id=测速批次|node_id=节点|node_ids=节点|label=名称|username=登录账号|password=密码|credentials=高级账号凭据|session_id=授权会话|code=授权码|url=链接|remote_id=平台作品编号|target_type=订阅类型|target=作者／标签／关键词|auto_download=自动下载正文|content_scope=内容范围|interval_minutes=检查间隔（分钟）|interval_hours=更新间隔（小时）|auto_update=自动更新|fetch_via_proxy=通过当前代理更新|proxy_url=代理地址|proxy_username=代理用户名|proxy_password=代理密码|controller_url=Clash 控制器地址|controller_secret=控制器密钥|group=策略组|node=节点名称|custom_url=自定义接口地址|admin_password=管理员密码|provider=模型类型|base_url=模型地址|model=模型名称|api_key=接口密钥|use_proxy=使用代理|consent=同意发送所选内容进行处理|force=重新处理已有结果|limit=数量上限|source=来源平台|type=内容类型|q=搜索关键词|status=状态|kind=任务类型|page=页码|page_size=每页数量|sort=排序|downloaded=仅已下载|favorite=仅收藏|history=仅浏览历史|favorite_group=收藏分组|tag=标签|author=作者|rating=内容分级|title=标题|description=介绍|tags=标签|paths=服务器相对路径|recursive=包含子目录|server_url=手机可访问的服务器地址|notes=更新说明|file=文件|files=文件|clear_api_key=清除已保存密钥|name=名称|items=作品列表|position=顺序|title_zh=中文标题|name_zh=中文名称|language=语言|created_at=创建时间|updated_at=更新时间|last_error=最近错误|error=错误|message=说明|progress=进度|result=结果|accounts=账号|subscriptions=订阅|nodes=节点|id=编号|total=总数|total_items=作品数|total_pages=文件数|total_bytes=总字节数|pending_jobs=待处理任务|untagged_items=无标签作品|source_counts=平台统计|rating_counts=分级统计|recent_items=最近作品|auth_status=登录状态|device_name=设备名称|expires_at=到期时间|last_used_at=最近使用|revoked_at=撤销时间|running=运行中|completed=已完成|failed=失败|queued=等待中|active=当前任务|lanes=各独立队列|token=凭据|access_token=授权凭据|qr_payload=绑定码|authorization_url=授权链接|login_url=授权链接|has_api_key=已保存密钥|effective_url=当前接口地址|default_url=默认接口地址|state=状态|healthy=健康|current=当前|content_state=正文状态|page_count=文件数|url_valid=地址有效|remote_page_count=平台总页数|content_job_id=正文任务|user_score=我的评分|is_favorite=已收藏|favorite_group_ids=收藏分组|group_ids=收藏分组|section=章节|offset=位置比例|rows=记录|label_zh=中文名称|service=服务|query=搜索内容|work_title=作品标题|candidate_name=人物名称|character_id=人物编号|character_name=人物名称|overwrite=覆盖已有资料
""".strip().split("|"))


def _group(path):
    if path.startswith(("/network",)):
        return "network"
    if path.startswith(("/sources", "/platform-endpoints")):
        return "sources"
    if path.startswith(("/jobs", "/queue")):
        return "queue"
    if path.startswith("/subscriptions"):
        return "subscriptions"
    if path.startswith(("/ai", "/translation")):
        return "ai"
    if path in ("/health", "/dashboard"):
        return "overview"
    if path.startswith(("/devices", "/database", "/app-updates", "/settings")):
        return "devices"
    return "library"


def _expand(value, components, depth=0):
    if depth > 12:
        return {"type": "string"}
    value = copy.deepcopy(value)
    if "$ref" in value:
        value = {**_expand(components[value.pop("$ref").split("/")[-1]], components, depth + 1), **value}
    if "anyOf" in value:
        options = value.pop("anyOf")
        value = {**_expand(next((s for s in options if s.get("type") != "null"), {}), components, depth + 1), **value,
                 "nullable": any(s.get("type") == "null" for s in options)}
    if "properties" in value:
        value["properties"] = {k: _expand(v, components, depth + 1) for k, v in value["properties"].items()}
    if "items" in value:
        value["items"] = _expand(value["items"], components, depth + 1)
    if isinstance(value.get("additionalProperties"), dict):
        value["additionalProperties"] = _expand(value["additionalProperties"], components, depth + 1)
    return value


def catalog(document):
    schemas = document.get("components", {}).get("schemas", {})
    actions = []
    for (method, path), title in TITLES.items():
        operation = document.get("paths", {}).get("/api" + path, {}).get(method.lower())
        if operation is None:
            continue
        parameters = []
        for value in operation.get("parameters", []):
            if value["in"] not in ("path", "query"):
                continue
            schema = _expand(value.get("schema", {}), schemas)
            if value["name"] in ("platform", "source") and "enum" not in schema:
                schema["enum"] = ["pixiv", "ehentai", "picacg", "jm", "kemono"]
                if value["name"] == "source":
                    schema["enum"].append("local")
            if value["name"] == "platform" and path == "/sources/{platform}/login":
                schema["enum"] = ["ehentai", "picacg", "jm"]
            if value["name"] == "platform" and path == "/sources/{platform}/sync":
                schema["enum"] = ["pixiv", "picacg"]
            if value["name"] == "platform" and path == "/sources/{platform}/accounts":
                schema["enum"] = ["pixiv", "ehentai", "picacg", "jm"]
            if value["name"] == "lane":
                schema["enum"] = ["download", "details", "checks", "ai", "translation", "other"]
            parameters.append({"name": value["name"], "location": value["in"],
                               "required": value.get("required", False), "schema": schema})
        content = operation.get("requestBody", {}).get("content", {})
        media = next(iter(content), None)
        body = _expand(content[media].get("schema", {}), schemas) if media else None
        if method == "POST" and path == "/app-updates/android":
            media, body = "application/vnd.android.package-archive", {"type": "string", "format": "binary"}
        actions.append({"id": method + " " + path, "method": method, "path": path,
                        "title": title, "group": _group(path), "parameters": parameters,
                        "body": body, "media_type": media,
                        "download": path.endswith("/export"),
                        "confirmation": method == "DELETE" or path in ("/translation/batch", "/network/managed/restart", "/app-updates/android")})
    return {"version": 1, "groups": GROUPS, "labels": LABELS, "actions": actions}


@router.get("/mobile-admin/catalog")
def management_catalog(request: Request):
    return catalog(request.app.openapi())
