import base64
from copy import deepcopy
import gzip
import ipaddress
import json
import socket
from urllib.parse import quote

import httpx
import pytest
import yaml

from app import proxy_subscription as subscription
from app.proxy_subscription import SubscriptionError, parse_subscription, validate_subscription_url

UUID = "12345678-1234-4321-8765-123456789abc"


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def basic_node(**changes):
    return {"name": "测试节点", "type": "ss", "server": "edge.example.com", "port": 443,
            "cipher": "aes-128-gcm", "password": "node-password", **changes}


def payload(*nodes):
    return yaml.safe_dump({"proxies": nodes or [basic_node()]}, allow_unicode=True).encode()


def test_clash_profile_imports_only_nodes_and_preserves_real_tls_fields():
    raw = {"proxies": [basic_node(type="tuic", token="v4-token", **{"disable-sni": True, "name-cert-verify": "tls.example.com"})],
        "mixed-port": 9999, "external-controller": "0.0.0.0:9090", "secret": "remote-secret",
        "dns": {"nameserver": ["http://localhost/private"]}, "proxy-providers": {"nested": {"url": "https://evil.invalid"}},
        "rules": ["MATCH,DIRECT"], "tun": {"enable": True}, "script": {"code": "must not run"}}
    nodes = parse_subscription(yaml.safe_dump(raw).encode())
    assert len(nodes) == 1
    assert nodes[0]["token"] == "v4-token"
    assert nodes[0]["disable-sni"] is True
    assert nodes[0]["name-cert-verify"] == "tls.example.com"
    assert not set(raw).intersection(nodes[0])


@pytest.mark.parametrize("auth", [b64("aes-128-gcm:p@ss:word"), "aes-128-gcm:" + quote("p@ss:word", safe="")])
def test_ss_sip002_percent_and_base64_credentials(auth):
    node = parse_subscription(f"ss://{auth}@edge.example.com:8443/?plugin=obfs-local%3Bobfs%3Dtls%3Bobfs-host%3Dcover.example.com#SS".encode())[0]
    assert node["password"] == "p@ss:word"
    assert node["port"] == 8443
    assert node["plugin"] == "obfs"
    assert node["plugin-opts"] == {"mode": "tls", "host": "cover.example.com"}


def test_ss_legacy_full_authority_and_2022_plain_credential():
    assert parse_subscription(("ss://" + b64("aes-128-gcm:pw@edge.example.com:443") + "#legacy").encode())[0]["password"] == "pw"
    node = parse_subscription(b"ss://2022-blake3-aes-128-gcm:c2VjcmV0%3D@edge.example.com:443#2022")[0]
    assert node["cipher"] == "2022-blake3-aes-128-gcm"
    assert node["password"] == "c2VjcmV0="


def test_vmess_base64_ws_and_http_camouflage_types():
    value = {"v": "2", "ps": "VMess", "add": "edge.example.com", "port": "443", "id": UUID,
             "aid": "0", "scy": "auto", "net": "ws", "path": "/ws", "host": "cdn.example.com",
             "tls": "tls", "sni": "tls.example.com", "alpn": "h2,http/1.1", "fp": "firefox"}
    node = parse_subscription(("vmess://" + b64(json.dumps(value))).encode())[0]
    assert node["ws-opts"] == {"path": "/ws", "headers": {"Host": "cdn.example.com"}}
    assert node["tls"] is True and node["client-fingerprint"] == "firefox"
    assert node["alpn"] == ["h2", "http/1.1"]
    value.update(net="tcp", type="http")
    node = parse_subscription(("vmess://" + b64(json.dumps(value))).encode())[0]
    assert node["http-opts"] == {"path": ["/ws"], "headers": {"Host": ["cdn.example.com"]}}


def test_vless_reality_tls_fingerprint_and_h2():
    uri = f"vless://{UUID}@edge.example.com:443?security=reality&pbk=publickey&sid=aabb&fp=firefox&flow=xtls-rprx-vision&type=grpc&serviceName=grpc&sni=tls.example.com#Reality"
    node = parse_subscription(uri.encode())[0]
    assert node["reality-opts"] == {"public-key": "publickey", "short-id": "aabb"}
    assert node["grpc-opts"] == {"grpc-service-name": "grpc"}
    assert node["client-fingerprint"] == "firefox" and node["tls"] is True
    uri = f"vless://{UUID}@edge.example.com:443?security=tls&fp=safari&type=http&path=%2Fh2&host=cdn.example.com#TLS"
    node = parse_subscription(uri.encode())[0]
    assert node["client-fingerprint"] == "safari"
    assert node["network"] == "h2" and node["h2-opts"]["path"] == "/h2"


def test_trojan_credentials_certificate_pin_and_websocket():
    pin = "ab" * 32
    node = parse_subscription(f"trojan://p%40ss%3Aword@edge.example.com:443?type=ws&path=%2Fws&sni=tls.example.com&fp=firefox&pcs={pin}&allowInsecure=0#Trojan".encode())[0]
    assert node["password"] == "p@ss:word"
    assert node["fingerprint"] == pin and node["client-fingerprint"] == "firefox"
    assert node["skip-cert-verify"] is False


def test_hysteria2_preserves_auth_obfuscation_and_tls_pin():
    pin = "ac" * 32
    node = parse_subscription(f"hy2://user:p%40ss@edge.example.com:8443/?sni=tls.example.com&obfs=salamander&obfs-password=obfs-secret&pinSHA256={pin}&insecure=0#HY2".encode())[0]
    assert node["type"] == "hysteria2" and node["password"] == "user:p@ss"
    assert node["obfs-password"] == "obfs-secret"
    assert node["fingerprint"] == pin and node["skip-cert-verify"] is False


def test_tuic_v4_and_v5_query_translation():
    uri = f"tuic://{UUID}:p%40ss@edge.example.com:443?congestion_control=bbr&udp_relay_mode=quic&disable_sni=1&reduce_rtt=0&allow_insecure=0&alpn=h3#TUIC5"
    node = parse_subscription(uri.encode())[0]
    assert node["uuid"] == UUID and node["password"] == "p@ss"
    assert node["congestion-controller"] == "bbr" and node["udp-relay-mode"] == "quic"
    assert node["disable-sni"] is True and node["reduce-rtt"] is False
    assert node["skip-cert-verify"] is False and node["alpn"] == ["h3"]
    node = parse_subscription(b"tuic://v4-token@edge.example.com:443#TUIC4")[0]
    assert node["token"] == "v4-token" and "uuid" not in node


def test_entire_uri_list_base64_with_whitespace():
    links = f"ss://{b64('aes-128-gcm:pw')}@edge.example.com:443#SS\n\nhy2://pw@edge.example.com:443#HY2"
    encoded = b64(links)
    encoded = "\n".join(encoded[index:index + 20] for index in range(0, len(encoded), 20))
    assert [node["type"] for node in parse_subscription(encoded.encode())] == ["ss", "hysteria2"]


@pytest.mark.parametrize("value", [b"x: &node {name: shared}\nproxies: [*node]", b"!!python/object/apply:os.system ['must-not-execute']",
    ("[" * 1000 + "1" + "]" * 1000).encode(), b"proxies: []", b"\xff\xfe", b"a" * (subscription.MAX_BYTES + 1)],
    ids=["yaml-alias", "python-tag", "deep-structure", "empty-proxies", "invalid-utf8", "over-size-limit"])
def test_unsafe_or_bounded_inputs_fail_with_safe_error(value):
    with pytest.raises(SubscriptionError) as error:
        parse_subscription(value)
    assert "must-not-execute" not in str(error.value)


@pytest.mark.parametrize("changes", [
    {"port": 65536}, {"port": True}, {"server": "edge.example.com/private"},
    {"plugin": "/bin/run-this"}, {"private-key": "private-value"},
    {"ws-opts": {"private-key-path": "/private/path"}},
    {"ech-opts": {"enable": True, "config": "unsupported-security"}}, {"up": float("nan")},
    {"network": "xhttp", "xhttp-opts": {"path": "/important"}}, {"realm-opts": {"enable": True, "token": "realm-secret"}},
])
def test_unsafe_node_fields_are_rejected(changes):
    with pytest.raises(SubscriptionError):
        parse_subscription(payload(basic_node(**changes)))


def test_node_count_and_duplicate_names_rejected():
    with pytest.raises(SubscriptionError, match="1000"):
        parse_subscription(payload(*(basic_node(name=str(index)) for index in range(1001))))
    with pytest.raises(SubscriptionError, match="重复"):
        parse_subscription(payload(basic_node(), basic_node()))


def test_unsupported_uri_security_is_not_silently_discarded():
    with pytest.raises(SubscriptionError, match="安全参数"):
        parse_subscription(b"hy2://pw@edge.example.com:443?ech=secret-ech#HY2")
    with pytest.raises(SubscriptionError, match="安全参数"):
        parse_subscription(b"trojan://pw@edge.example.com:443?security=reality#Trojan")


def public_dns(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


@pytest.mark.parametrize("url", ["http://provider.example/sub", "https://provider.example:8443/sub", "https://user:pw@provider.example/sub",
    "https://provider.example/sub#fragment", "https://127.0.0.1/sub", "https://[::1]/sub", "https://localhost/sub",
    "https://service.local/sub", "https://localhost.localdomain/sub", "https://127.1/sub", "https://0x7f000001/sub"])
def test_subscription_url_rejects_private_or_non_https_even_through_proxy(url):
    with pytest.raises(SubscriptionError):
        validate_subscription_url(url, resolve=False)


def test_direct_dns_checks_all_addresses_but_proxy_does_not_use_local_fake_ip(monkeypatch):
    dns = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.1", 443))]
    monkeypatch.setattr(subscription.socket, "getaddrinfo", dns)
    checked = []
    def doh(host):
        checked.append(host)
        return [ipaddress.ip_address("8.8.8.8")]
    monkeypatch.setattr(subscription, "_doh_addresses", doh)
    assert validate_subscription_url("https://provider.example/sub")
    assert checked == ["provider.example"]
    assert validate_subscription_url("https://provider.example/sub", resolve=False)
    assert checked == ["provider.example"]
    monkeypatch.setattr(subscription.socket, "getaddrinfo", lambda *a, **kw: public_dns() + dns())
    assert validate_subscription_url("https://provider.example/sub")
    assert checked == ["provider.example", "provider.example"]


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.1.1", "10.0.0.1", "172.16.1.2", "::1", "fc00::1", "224.0.0.1", "169.254.169.254"])
def test_real_private_or_multicast_dns_is_never_treated_as_fake_ip(monkeypatch, address):
    monkeypatch.setattr(subscription.socket, "getaddrinfo", lambda *a, **kw: public_dns() + [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.1", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    monkeypatch.setattr(subscription, "_doh_addresses", lambda host: pytest.fail("Private DNS must fail before DoH"))
    with pytest.raises(SubscriptionError):
        validate_subscription_url("https://provider.example/sub")


@pytest.mark.parametrize("url", ["https://198.18.0.1/sub", "https://[2001:2::73]/sub", "https://224.0.0.1/sub"])
def test_fake_and_multicast_ip_literals_remain_forbidden(url):
    with pytest.raises(SubscriptionError):
        validate_subscription_url(url, resolve=False)


def mock_fetch(monkeypatch, handler):
    from app import network
    client_type = httpx.Client
    options = []

    def factory(**kwargs):
        options.append(kwargs)
        return client_type(transport=httpx.MockTransport(handler), follow_redirects=False, headers=kwargs["headers"])

    monkeypatch.setattr(subscription.socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(subscription.httpx, "Client", factory)
    monkeypatch.setattr(network, "httpx_proxy", lambda: "http://127.0.0.1:7890")
    return options


def response_bytes(content, *, headers=None, status=200):
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(content))


def test_https_fetch_handles_same_origin_redirect_and_never_inherits_environment(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.headers["accept-encoding"] == "identity"
        if len(calls) == 1:
            return response_bytes(b"", status=302, headers={"location": "/final?token=secret-token"})
        return response_bytes(payload())

    options = mock_fetch(monkeypatch, handler)
    assert subscription.fetch_subscription("https://provider.example/sub?token=secret-token")[0]["type"] == "ss"
    assert len(calls) == 2
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert options[0]["proxy"] is None


def test_proxy_fetch_delegates_dns_and_uses_current_proxy(monkeypatch):
    options = mock_fetch(monkeypatch, lambda request: response_bytes(payload()))
    monkeypatch.setattr(subscription.socket, "getaddrinfo", lambda *a, **kw: pytest.fail("Proxy mode must delegate DNS"))
    assert subscription.fetch_subscription("https://provider.example/sub", via_proxy=True)
    assert options[0]["proxy"] == "http://127.0.0.1:7890"


@pytest.mark.parametrize("location", ["https://other.example/sub?token=secret-token", "http://provider.example/sub?token=secret-token", "https://provider.example:8443/sub"])
def test_fetch_rechecks_redirect_and_never_leaks_url_secrets(monkeypatch, location):
    calls = []

    def handler(request):
        calls.append(request)
        return response_bytes(b"", status=302, headers={"location": location})

    mock_fetch(monkeypatch, handler)
    with pytest.raises(SubscriptionError) as error:
        subscription.fetch_subscription("https://provider.example/sub?token=secret-token")
    assert len(calls) == 1
    assert "secret-token" not in str(error.value)


@pytest.mark.parametrize("compress", [False, True])
def test_fetch_limits_wire_and_expanded_size(monkeypatch, compress):
    content = b"a" * (subscription.MAX_BYTES + 1)
    if compress:
        content = gzip.compress(content)
    mock_fetch(monkeypatch, lambda request: response_bytes(content, headers={"content-encoding": "gzip"} if compress else None))
    with pytest.raises(SubscriptionError, match="2 MiB"):
        subscription.fetch_subscription("https://provider.example/sub")


def test_fetch_supports_gzip_but_rejects_truncated_gzip(monkeypatch):
    content = gzip.compress(payload())
    mock_fetch(monkeypatch, lambda request: response_bytes(content, headers={"content-encoding": "gzip"}))
    assert subscription.fetch_subscription("https://provider.example/sub")[0]["name"] == "测试节点"
    # The handler captures the reassigned variable.
    content = content[:-4]
    with pytest.raises(SubscriptionError, match="不完整"):
        subscription.fetch_subscription("https://provider.example/sub")


def test_http_failure_does_not_echo_upstream_body_or_url(monkeypatch):
    mock_fetch(monkeypatch, lambda request: response_bytes(b"secret-body", status=403))
    with pytest.raises(SubscriptionError) as error:
        subscription.fetch_subscription("https://provider.example/sub?token=secret-token")
    assert "403" in str(error.value)
    assert "secret" not in str(error.value)


def test_node_ids_separate_same_prefix_subscriptions_and_survive_reorder():
    from app.managed_proxy import mapped_nodes

    definitions = [basic_node(name="同名"), basic_node(name="另一节点")]
    first = mapped_nodes("12345678aaaaaaaa", definitions)
    other = mapped_nodes("12345678bbbbbbbb", definitions)
    assert {node["id"] for node in first}.isdisjoint({node["id"] for node in other})
    first[0]["delay"] = 123
    repeated = mapped_nodes("12345678aaaaaaaa", list(reversed(definitions)), first)
    assert {node["name"]: node["id"] for node in repeated} == {node["name"]: node["id"] for node in first}
    assert next(node for node in repeated if node["name"] == "同名")["delay"] == 123
    changed = deepcopy(definitions)
    changed[0]["password"] = "new-password"
    assert mapped_nodes("12345678aaaaaaaa", changed, first)[0]["delay"] is None


def doh_payload(host, record_type, addresses):
    return json.dumps({"Status": 0, "TC": False, "Question": {"name": host + ".", "type": record_type},
        "Answer": [{"name": host, "type": record_type, "data": address} for address in addresses]}).encode()


def mock_fake_ip_fetch(monkeypatch, handler):
    options = mock_fetch(monkeypatch, handler)
    lookups = []
    def fake_dns(host, *args, **kwargs):
        lookups.append(host)
        assert host == "provider.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.124", 443)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:2::73", 443, 0, 0))]
    monkeypatch.setattr(subscription.socket, "getaddrinfo", fake_dns)
    return options, lookups


def test_fake_ip_doh_checks_both_families_and_pins_target_preserving_host_tls(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == "223.5.5.5":
            assert request.headers["host"] == "dns.alidns.com"
            assert request.extensions["sni_hostname"] == "dns.alidns.com"
            assert set(request.url.params) == {"name", "type"}
            assert request.url.params["name"] == "provider.example"
            assert "secret-token" not in str(request.url)
            kind = int(request.url.params["type"])
            return response_bytes(doh_payload("provider.example", kind, ["8.8.8.8"] if kind == 1 else ["2606:4700:4700::1111"]))
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == "provider.example"
        assert request.extensions["sni_hostname"] == "provider.example"
        assert request.url.params["token"] == "secret-token"
        return response_bytes(payload())
    options, lookups = mock_fake_ip_fetch(monkeypatch, handler)
    assert subscription.fetch_subscription("https://provider.example/sub?token=secret-token")
    assert lookups == ["provider.example"]
    assert len(calls) == 3
    assert all(option["trust_env"] is False and option["follow_redirects"] is False
               and option["proxy"] is None and option.get("verify", True) is not False for option in options)


@pytest.mark.parametrize("address", ["10.0.0.1", "127.0.0.1", "169.254.169.254", "198.18.0.124", "2001:2::73", "fc00::1", "224.0.0.1"])
def test_fake_ip_doh_private_answers_fail_closed_without_trying_another_resolver(monkeypatch, address):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.host == "223.5.5.5"
        return response_bytes(doh_payload("provider.example", 1, [address]))
    mock_fake_ip_fetch(monkeypatch, handler)
    with pytest.raises(SubscriptionError, match="非公网") as error:
        subscription.fetch_subscription("https://provider.example/sub?token=secret-token")
    assert len(calls) == 1
    assert "secret-token" not in str(error.value)


def test_fake_ip_doh_unavailable_fails_without_connecting_to_fake_or_original_host(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.host in {"223.5.5.5", "1.1.1.1"}
        raise httpx.ConnectError("private-upstream-debug-secret")
    mock_fake_ip_fetch(monkeypatch, handler)
    with pytest.raises(SubscriptionError, match="fake-IP") as error:
        subscription.fetch_subscription("https://provider.example/sub?token=secret-token")
    assert len(calls) == 2
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("data", [b"not-json", b"[]", b'{"Status":3}', b'{"Status":0,"Question":[]}', b"x" * 65537],
    ids=["non-json", "not-object", "nxdomain", "wrong-question", "oversized"])
def test_fake_ip_doh_invalid_or_oversized_reply_rejected(monkeypatch, data):
    mock_fake_ip_fetch(monkeypatch, lambda request: response_bytes(data))
    with pytest.raises(SubscriptionError, match="核验未完成"):
        subscription.fetch_subscription("https://provider.example/sub?token=secret-token")


def test_fake_ip_doh_does_not_follow_redirects(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return response_bytes(b"", status=302, headers={"location": "https://127.0.0.1/"})
    mock_fake_ip_fetch(monkeypatch, handler)
    with pytest.raises(SubscriptionError):
        subscription.fetch_subscription("https://provider.example/sub")
    assert [request.url.host for request in calls] == ["223.5.5.5", "1.1.1.1"]


@pytest.mark.parametrize("certificate_host", ["provider.example", "wrong.example"])
def test_pinned_transport_uses_real_tls_hostname_verification(monkeypatch, tmp_path, certificate_host):
    """Real local TLS server; only TCP routing and the test trust root are replaced."""
    import datetime
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import ssl
    import threading
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from httpcore._backends.sync import SyncBackend

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, certificate_host)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName(certificate_host)]), False)
        .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    sni = []
    server_context.set_servername_callback(lambda sock, name, context: sni.append(name))
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.headers["host"], self.path))
            body = payload()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_connect = SyncBackend.connect_tcp
    connections = []
    def connect(self, host, port, **kwargs):
        connections.append((host, port))
        assert host == "8.8.8.8" and port == 443
        return original_connect(self, "127.0.0.1", server.server_port, **kwargs)
    monkeypatch.setattr(SyncBackend, "connect_tcp", connect)
    monkeypatch.setattr(subscription, "_subscription_addresses", lambda url: [ipaddress.ip_address("8.8.8.8")])
    client_type = httpx.Client
    def factory(**kwargs):
        kwargs["verify"] = ssl.create_default_context(cafile=str(cert_path))
        return client_type(**kwargs)
    monkeypatch.setattr(subscription.httpx, "Client", factory)
    try:
        if certificate_host == "provider.example":
            assert subscription.fetch_subscription("https://provider.example/sub?token=secret-token")
            assert seen == [("provider.example", "/sub?token=secret-token")]
        else:
            with pytest.raises(SubscriptionError, match="连接失败"):
                subscription.fetch_subscription("https://provider.example/sub?token=secret-token")
            assert seen == []  # HTTP credentials are never sent after wrong TLS.
        assert connections == [("8.8.8.8", 443)]
        assert sni == ["provider.example"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
