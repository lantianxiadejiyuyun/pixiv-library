"""Bounded, local conversion of subscription contents; no online converter.

Only node definitions enter the managed core. Remote DNS, listeners, providers,
rules, file paths and executable profile settings are never applied.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import math
import re
import socket
import zlib
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx
import yaml

MAX_BYTES = 2 * 1024 * 1024
MAX_NODES = 1000
# Only benchmark ranges observed in fake-IP DNS can trigger an independent
# lookup. These ranges are NEVER accepted as an actual destination or URL IP.
FAKE_IP_RANGES = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48"))
# Fixed IPs avoid needing the very system DNS that returned fake-IP addresses.
# AliDNS JSON API: https://www.alibabacloud.com/help/en/dns/json-api-for-doh
# Cloudflare JSON API: https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/
DOH_RESOLVERS = (("223.5.5.5", "dns.alidns.com", "/resolve"),
                 ("1.1.1.1", "cloudflare-dns.com", "/dns-query"))
SUPPORTED = {"ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2", "tuic", "http", "socks5", "anytls", "snell"}
# Protocol transport options are passed to Mihomo validation, but no file/plugin
# execution, provider expansion, dialer references or interface binding is allowed.
NODE_KEYS = set("name type server port password username cipher uuid alterId security udp tls servername sni alpn skip-cert-verify fingerprint client-fingerprint network ws-opts grpc-opts http-opts h2-opts reality-opts flow packet-encoding encryption plugin plugin-opts obfs obfs-param protocol protocol-param auth auth-str up down ports hop-interval recv-window-conn recv-window disable-mtu-discovery fast-open obfs-password congestion-controller udp-relay-mode reduce-rtt heartbeat-interval request-timeout version idle-session-check-interval idle-session-timeout min-idle-session udp-over-tcp udp-over-tcp-version smux tfo ip-version dialer-proxy".split())
NODE_KEYS.discard("dialer-proxy")
NODE_KEYS.update({"token", "disable-sni", "name-cert-verify"})
FORBIDDEN_KEYS = {"certificate", "certificate-path", "private-key", "private-key-path", "ca", "ca-str", "interface-name", "routing-mark", "dialer-proxy", "proxy-provider", "path-file"}
UNSUPPORTED_TLS_KEYS = {"ech-opts", "shadow-tls-opts", "restls-opts", "jls-opts", "tlsmirror-opts"}


class SubscriptionError(ValueError):
    pass


def _plain_tree(value, depth=0):
    if depth > 12:
        raise SubscriptionError("订阅节点结构过深")
    if isinstance(value, float) and not math.isfinite(value):
        raise SubscriptionError("订阅节点含无效数字")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 16384 or any(ord(c) < 32 and c not in "\t\n" for c in value):
            raise SubscriptionError("订阅节点含无效或过长字段")
        return value
    if isinstance(value, list) and len(value) <= 200:
        return [_plain_tree(v, depth + 1) for v in value]
    if isinstance(value, dict) and len(value) <= 100:
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or key in FORBIDDEN_KEYS:
                raise SubscriptionError("订阅节点包含不支持的本地文件或代理链配置")
            result[key] = _plain_tree(item, depth + 1)
        return result
    raise SubscriptionError("订阅节点结构或字段类型不受支持")


def sanitize_node(node):
    if not isinstance(node, dict):
        raise SubscriptionError("订阅中的节点必须是对象")
    if node.get("type") not in SUPPORTED:
        raise SubscriptionError("订阅含不支持的节点协议，请提供 Clash/Mihomo 常用代理节点订阅")
    if any(key in node for key in FORBIDDEN_KEYS):
        raise SubscriptionError("不支持引用本地文件、网卡或其他代理的节点")
    if any(key in node for key in UNSUPPORTED_TLS_KEYS):
        raise SubscriptionError("订阅含暂不支持的 TLS 扩展，未忽略其安全参数；请使用常规节点配置")
    if "realm-opts" in node or node.get("network", "tcp") not in {"tcp", "raw", "ws", "grpc", "http", "h2"}:
        raise SubscriptionError("订阅含暂不支持的传输扩展，未忽略连接参数；请使用常规节点配置")
    name = node.get("name")
    server = node.get("server")
    if not isinstance(name, str) or not name.strip() or len(name) > 256 or any(ord(c) < 32 for c in name):
        raise SubscriptionError("节点名称为空、过长或包含控制字符")
    if not isinstance(server, str) or not server or len(server) > 253 or any(c.isspace() or c in "/\\@?#" for c in server):
        raise SubscriptionError("节点服务器地址无效")
    port = node.get("port")
    if isinstance(port, bool) or not str(port).isdigit() or not 1 <= int(port) <= 65535:
        raise SubscriptionError("节点端口无效")
    if node.get("plugin") not in (None, "obfs", "v2ray-plugin", "shadow-tls", "restls"):
        raise SubscriptionError("不支持该节点插件；不执行外部插件程序")
    result = _plain_tree({k: v for k, v in node.items() if k in NODE_KEYS})
    result.update(name=name.strip(), port=int(port))
    if result.get("network") == "raw":
        result["network"] = "tcp"
    return result


def _b64(value):
    try:
        encoded = value.encode("ascii")
        return base64.b64decode(encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        raise SubscriptionError("订阅 Base64 内容无效") from None


def _transport(node, query):
    network = query.get("type", query.get("network", "tcp"))
    if network in ("tcp", "raw", "none", ""):
        return
    if network == "ws":
        node["network"] = "ws"
        node["ws-opts"] = {"path": query.get("path", "/")}
        if query.get("host"):
            node["ws-opts"]["headers"] = {"Host": query["host"]}
    elif network == "grpc":
        node["network"] = "grpc"
        node["grpc-opts"] = {"grpc-service-name": query.get("serviceName", "")}
    elif network == "h2":
        node["network"] = "h2"
        node["h2-opts"] = {"path": query.get("path", "/"), "host": [query.get("host") or node["server"]]}
    elif network == "http":
        node["network"] = "http"
        node["http-opts"] = {"path": [query.get("path", "/")]}
        if query.get("host"):
            node["http-opts"]["headers"] = {"Host": [query["host"]]}
    else:
        raise SubscriptionError("URI 订阅包含暂不支持的传输格式，请改用 Clash YAML 订阅")


def _uri_bool(value):
    if str(value).lower() in {"1", "true"}:
        return True
    if str(value).lower() in {"0", "false"}:
        return False
    raise SubscriptionError("节点链接包含无效布尔参数")


def parse_uri(uri):
    try:
        if uri.startswith("vmess://"):
            value = json.loads(_b64(uri[8:]))
            node = {"name": value.get("ps") or "VMess", "type": "vmess", "server": value["add"],
                "port": value["port"], "uuid": value["id"], "alterId": int(value.get("aid", 0)),
                "cipher": value.get("scy") or "auto", "tls": value.get("tls") == "tls", "udp": True}
            if value.get("sni"):
                node["servername"] = value["sni"]
            if value.get("alpn"):
                node["alpn"] = value["alpn"].split(",")
            if value.get("fp"):
                node["client-fingerprint"] = value["fp"]
            network = value.get("net", "tcp")
            if value.get("type") == "http":
                network = "http"
            elif network == "http":
                network = "h2"
            _transport(node, {"type": network, "host": value.get("host", ""), "path": value.get("path", "/"), "serviceName": value.get("path", "")})
            return node
        parsed = urlsplit(uri)
        query = {k: v[-1] for k, v in parse_qs(parsed.query, max_num_fields=40).items()}
        if any(key.lower() in {"ech", "echconfig", "echconfiglist", "certificate", "private-key", "ca"} for key in query):
            raise SubscriptionError("该链接包含暂不支持的 TLS 配置，不会忽略安全参数；请使用受支持的节点格式")
        name = unquote(parsed.fragment) or parsed.scheme.upper()
        if parsed.scheme == "ss":
            authority = uri[5:].split("#", 1)[0].split("?", 1)[0].rstrip("/")
            if "@" not in authority:
                authority = _b64(authority)
            auth, endpoint = authority.rsplit("@", 1)
            auth = unquote(auth)
            if ":" not in auth:
                auth = _b64(auth)
            cipher, password = auth.split(":", 1)
            endpoint = urlsplit("ss://" + endpoint)
            node = {"name": name, "type": "ss", "server": endpoint.hostname, "port": endpoint.port, "cipher": cipher, "password": password, "udp": True}
            if query.get("plugin"):
                parts = query["plugin"].split(";")
                plugin = parts[0]
                node["plugin"] = "obfs" if plugin in ("obfs-local", "simple-obfs") else plugin
                opts = dict(part.split("=", 1) if "=" in part else (part, True) for part in parts[1:] if part)
                if node["plugin"] == "obfs":
                    opts = {"mode": opts.get("obfs", "http"), "host": opts.get("obfs-host", "")}
                node["plugin-opts"] = opts
            return node
        kind = {"hy2": "hysteria2"}.get(parsed.scheme, parsed.scheme)
        if kind not in {"vless", "trojan", "hysteria2", "tuic"}:
            raise SubscriptionError("URI 订阅协议暂不支持，请使用 Clash YAML 格式")
        node = {"name": name, "type": kind, "server": parsed.hostname, "port": parsed.port or 443, "udp": True}
        auth = unquote(parsed.username or "")
        if kind == "tuic" and parsed.password is None:
            node["token"] = auth  # TUIC v4; v5 uses UUID plus password.
        elif kind in ("vless", "tuic"):
            node["uuid"] = auth
            if kind == "tuic":
                node["password"] = unquote(parsed.password or "")
        else:
            node["password"] = auth + ((":" + unquote(parsed.password)) if parsed.password else "")
        if query.get("sni") or query.get("peer"):
            node["sni" if kind != "vless" else "servername"] = query.get("sni") or query["peer"]
        if query.get("alpn"):
            node["alpn"] = query["alpn"].split(",")
        for key in ("insecure", "allowInsecure", "allow_insecure"):
            if key in query:
                node["skip-cert-verify"] = _uri_bool(query[key])
                break
        if query.get("pinSHA256") or query.get("pcs"):
            node["fingerprint"] = query.get("pinSHA256") or query["pcs"]
        if kind in {"vless", "trojan"} and query.get("fp"):
            node["client-fingerprint"] = query["fp"]
        if kind == "vless":
            if query.get("security", "none") not in {"none", "tls", "reality"}:
                raise SubscriptionError("VLESS TLS 安全模式暂不支持")
            node["tls"] = query.get("security") in ("tls", "reality")
            if query.get("encryption"):
                node["encryption"] = query["encryption"]
            if query.get("flow"):
                node["flow"] = query["flow"]
            if query.get("security") == "reality":
                node["reality-opts"] = {"public-key": query.get("pbk", ""), "short-id": query.get("sid", "")}
                node["client-fingerprint"] = query.get("fp", "chrome")
        if kind in ("vless", "trojan"):
            # VLESS share links use type=http for HTTP/2, unlike VMess's
            # explicit HTTP/1 camouflage header field.
            transport = {**query, "type": "h2"} if kind == "vless" and query.get("type") == "http" else query
            _transport(node, transport)
        if kind == "trojan" and query.get("security", "tls") != "tls":
            raise SubscriptionError("此 Trojan TLS 模式暂不支持，不会忽略安全参数")
        if kind == "hysteria2" and query.get("obfs"):
            node.update(obfs=query["obfs"], **{"obfs-password": query.get("obfs-password", "")})
        if kind == "hysteria2":
            for key in ("up", "down"):
                if query.get(key):
                    node[key] = query[key]
        if kind == "tuic":
            for uri_key, node_key in (("congestion_control", "congestion-controller"), ("udp_relay_mode", "udp-relay-mode")):
                if query.get(uri_key):
                    node[node_key] = query[uri_key]
            for uri_key, node_key in (("disable_sni", "disable-sni"), ("reduce_rtt", "reduce-rtt")):
                if uri_key in query:
                    node[node_key] = _uri_bool(query[uri_key])
        return node
    except SubscriptionError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise SubscriptionError("节点链接无效或格式暂不支持，请改用 Clash YAML 订阅") from None


def parse_subscription(content):
    if not content or len(content) > MAX_BYTES:
        raise SubscriptionError("订阅为空或超过 2 MiB 限制")
    try:
        text = content.decode("utf-8-sig").strip()
    except UnicodeError:
        raise SubscriptionError("订阅不是有效 UTF-8 文本") from None
    # Reject aliases before construction to prevent exponential alias expansion.
    try:
        events = yaml.parse(text)
        count = 0
        depth = 0
        for event in events:
            count += 1
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                if depth > 16:
                    raise SubscriptionError("订阅 YAML 结构过深")
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
            if isinstance(event, yaml.events.AliasEvent) or count > 150000:
                raise SubscriptionError("订阅 YAML 不支持别名或过大的结构")
        value = yaml.safe_load(text)
    except yaml.YAMLError:
        value = None
    if isinstance(value, dict):
        nodes = value.get("proxies")
        if not isinstance(nodes, list):
            raise SubscriptionError("订阅没有 proxies 节点列表；不导入远端规则或嵌套订阅")
    else:
        if "://" not in text:
            text = _b64(re.sub(r"\s+", "", text))
        links = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if len(links) > MAX_NODES:
            raise SubscriptionError("单个订阅最多支持 1000 个节点")
        nodes = [parse_uri(link) for link in links]
    if not nodes or len(nodes) > MAX_NODES:
        raise SubscriptionError("订阅节点为空或超过 1000 个限制")
    result = [sanitize_node(node) for node in nodes]
    if len({node["name"] for node in result}) != len(result):
        raise SubscriptionError("订阅中存在重复节点名称，请在订阅端为节点设置不同名称")
    return result


def validate_subscription_url(url, *, resolve=True):
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment
                or len(url) > 4096 or parts.port not in (None, 443)
                or any(c.isspace() or ord(c) < 32 or c == "\\" for c in url)):
            raise ValueError()
        host = parts.hostname.rstrip(".").lower()
        if "." not in host and ":" not in host or host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".lan", ".internal", ".home", ".localdomain")):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                # inet_aton only parses numeric IPv4 forms; it does not query DNS.
                # This also blocks shortened/hex loopback forms through a proxy.
                address = ipaddress.ip_address(socket.inet_aton(host))
            except OSError:
                address = None
        if address is not None and not _public_address(address):
            raise ValueError()
    except (ValueError, OSError):
        raise SubscriptionError("订阅地址须为可解析的公网 HTTPS 链接（443 端口）") from None
    if resolve:
        _subscription_addresses(url)
    return url


def _public_address(address):
    return address.is_global and not address.is_multicast and not getattr(address, "scope_id", None)


def _fake_address(address):
    return any(address.version == subnet.version and address in subnet for subnet in FAKE_IP_RANGES)


class _DoHUnavailable(Exception):
    pass


def _doh_answer_addresses(data, host, record_type):
    if not isinstance(data, dict) or type(data.get("Status")) is not int or data["Status"] != 0 or data.get("TC") is True:
        raise _DoHUnavailable()
    question = data.get("Question")
    question = [question] if isinstance(question, dict) else question
    if (not isinstance(question, list) or len(question) != 1 or not isinstance(question[0], dict)
            or question[0].get("name", "").rstrip(".").lower() != host.rstrip(".").lower()
            or question[0].get("type") != record_type):
        raise _DoHUnavailable()
    answers = data.get("Answer") or []
    if not isinstance(answers, list) or len(answers) > 128:
        raise _DoHUnavailable()
    addresses = []
    for answer in answers:
        if not isinstance(answer, dict):
            raise _DoHUnavailable()
        if answer.get("type") not in (1, 28):
            continue  # CNAME and DNSSEC metadata are not connection addresses.
        try:
            address = ipaddress.ip_address(answer.get("data", ""))
        except ValueError:
            raise _DoHUnavailable() from None
        if not _public_address(address):
            raise SubscriptionError("订阅域名的公网 DNS 核验返回非公网地址，已拒绝连接")
        addresses.append(address)
    return addresses


def _doh_addresses(host):
    """Query only a hostname, never a subscription path, token, or credential."""
    for ip, resolver_host, path in DOH_RESOLVERS:
        try:
            with httpx.Client(proxy=None, timeout=4, trust_env=False, follow_redirects=False,
                              headers={"Host": resolver_host, "Accept": "application/dns-json", "Accept-Encoding": "identity"}) as client:
                addresses = []
                for record_type in (1, 28):
                    with client.stream("GET", f"https://{ip}{path}",
                            params={"name": host, "type": record_type},
                            extensions={"sni_hostname": resolver_host}) as response:
                        if response.status_code != 200 or response.headers.get("content-encoding", "identity") not in ("", "identity"):
                            raise _DoHUnavailable()
                        body = bytearray()
                        for chunk in response.iter_raw(chunk_size=8192):
                            body.extend(chunk)
                            if len(body) > 65536:
                                raise _DoHUnavailable()
                    addresses.extend(_doh_answer_addresses(json.loads(body), host, record_type))
                if addresses:
                    return sorted(set(addresses), key=lambda value: (value.version, int(value)))
        except SubscriptionError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, _DoHUnavailable):
            continue
    raise SubscriptionError("检测到 fake-IP DNS，但公网 DNS 核验未完成；请检查网络或选择通过已启用代理下载")


def _subscription_addresses(url):
    validate_subscription_url(url, resolve=False)
    try:
        host = httpx.URL(url).raw_host.decode("ascii").rstrip(".")
        try:
            address = ipaddress.ip_address(host)
            return [address]  # Literal public IP was already checked above.
        except ValueError:
            pass
        entries = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = {ipaddress.ip_address(entry[4][0]) for entry in entries}
        if not addresses or len(addresses) > 128:
            raise ValueError()
        # An ordinary private/loopback answer must never trigger the fallback,
        # even when the same response also contains a fake or public IP.
        if any(not _public_address(address) and not _fake_address(address) for address in addresses):
            raise ValueError()
        if any(_fake_address(address) for address in addresses):
            return _doh_addresses(host)
        return sorted(addresses, key=lambda value: (value.version, int(value)))
    except SubscriptionError:
        raise
    except (ValueError, OSError, httpx.InvalidURL):
        raise SubscriptionError("订阅地址须为可解析的公网 HTTPS 链接（443 端口）") from None


def _pinned_subscription_target(url):
    addresses = _subscription_addresses(url)
    original = httpx.URL(url)
    # This documented HTTPCore extension keeps certificate hostname validation
    # and SNI while connecting to a checked IP, avoiding DNS re-resolution.
    # https://www.encode.io/httpcore/extensions/#sni_hostname
    return (original.copy_with(host=str(addresses[0])), {"Host": original.netloc.decode("ascii")},
            {"sni_hostname": original.raw_host.decode("ascii").rstrip(".")})


def _bounded_body(response):
    """Bound both wire and expanded bytes, including compressed subscriptions."""
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "", "gzip", "deflate"}:
        raise SubscriptionError("订阅压缩格式暂不支持，请提供普通文本或 gzip 订阅")
    decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding in {"gzip", "deflate"} else None
    body = bytearray()
    received = 0
    try:
        for chunk in response.iter_raw(chunk_size=65536):
            received += len(chunk)
            if received > MAX_BYTES:
                raise SubscriptionError("订阅超过 2 MiB 限制")
            body.extend(decoder.decompress(chunk, MAX_BYTES + 1 - len(body)) if decoder else chunk)
            if len(body) > MAX_BYTES:
                raise SubscriptionError("订阅超过 2 MiB 限制")
            if decoder and decoder.unused_data:
                raise SubscriptionError("订阅压缩数据含不支持的附加内容")
        if decoder and not decoder.eof:
            raise SubscriptionError("订阅压缩数据不完整")
    except zlib.error:
        raise SubscriptionError("订阅压缩数据无效") from None
    return bytes(body)


def fetch_subscription(url, via_proxy=False):
    from .network import httpx_proxy
    proxy = httpx_proxy() if via_proxy else None
    if via_proxy and not proxy:
        raise SubscriptionError("尚未启用可用代理，请先直连导入订阅或启用已有代理")
    try:
        with httpx.Client(proxy=proxy, timeout=25, trust_env=False, follow_redirects=False,
                          headers={"User-Agent": "clash.meta/qiye-library", "Accept": "application/yaml,text/plain,*/*", "Accept-Encoding": "identity"}) as client:
            for _ in range(4):
                validate_subscription_url(url, resolve=False)
                if proxy:
                    target, headers, extensions = url, {}, {}
                else:
                    target, headers, extensions = _pinned_subscription_target(url)
                with client.stream("GET", target, headers=headers, extensions=extensions) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = urljoin(url, response.headers.get("location", ""))
                        # Relative/same-host subscription redirects are common. Cross-host
                        # redirects could expose a secret token to an unapproved recipient.
                        if urlsplit(target).hostname != urlsplit(url).hostname:
                            raise SubscriptionError("订阅跳转到其他域名，请填写提供商最终订阅链接")
                        url = target
                        continue
                    if response.status_code != 200:
                        raise SubscriptionError(f"订阅下载失败（HTTP {response.status_code}），请检查链接或订阅有效期")
                    return parse_subscription(_bounded_body(response))
            raise SubscriptionError("订阅重定向过多")
    except httpx.HTTPError:
        raise SubscriptionError("订阅连接失败，请检查网络或更换订阅下载方式；原节点保持不变") from None
