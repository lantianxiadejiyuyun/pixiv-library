import 'dart:convert';
import 'dart:io';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// QR codes accept only a server root. Never interpret them as deep links or code.
Uri validateServer(String raw) {
  final value = raw.trim();
  if (value.length > 2048 || RegExp(r'[\x00-\x20\x7f\\?#]').hasMatch(value)) {
    throw const FormatException('服务器地址格式无效');
  }
  final uri = Uri.tryParse(value);
  if (uri == null ||
      !['http', 'https'].contains(uri.scheme) ||
      uri.host.isEmpty ||
      uri.userInfo.isNotEmpty ||
      !['', '/'].contains(uri.path) ||
      uri.port < 1 ||
      uri.port > 65535 ||
      uri.host.contains('%')) {
    throw const FormatException('请使用不带路径和账号的服务器根地址');
  }
  final host = uri.host.toLowerCase().replaceAll('[', '').replaceAll(']', '');
  final ip = InternetAddress.tryParse(host);
  bool local = false;
  if (ip != null) {
    final bytes = ip.rawAddress;
    final ipv4 = ip.type == InternetAddressType.IPv4;
    if (ip.isMulticast || bytes.every((v) => v == 0)) {
      throw const FormatException('服务器地址无效');
    }
    local =
        ip.isLoopback ||
        ip.isLinkLocal ||
        (ipv4
            ? bytes[0] == 10 ||
                  (bytes[0] == 172 && bytes[1] >= 16 && bytes[1] <= 31) ||
                  (bytes[0] == 192 && bytes[1] == 168)
            : (bytes[0] & 0xfe) == 0xfc);
  } else {
    if (host.length > 253 ||
        !host
            .split('.')
            .every(
              (p) =>
                  RegExp(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$').hasMatch(p),
            ) ||
        RegExp(
          r'^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*$',
        ).hasMatch(host)) {
      throw const FormatException('服务器主机名无效');
    }
    local =
        !host.contains('.') ||
        host.endsWith('.local') ||
        host.endsWith('.lan') ||
        host.endsWith('.home.arpa');
  }
  if (uri.scheme == 'http' && !local) {
    throw const FormatException('公网服务器必须使用 HTTPS');
  }
  return uri.replace(path: '');
}

class PairingCode {
  final Uri server;
  final String token;
  const PairingCode(this.server, this.token);
  factory PairingCode.parse(String raw) {
    if (raw.length > 4096) throw const FormatException('不是栖页绑定码');
    dynamic value;
    try {
      value = jsonDecode(raw);
    } catch (_) {
      throw const FormatException('不是栖页绑定码');
    }
    if (value is! Map ||
        value['type'] != 'qiye-library' ||
        value['version'] != 1 ||
        value['server_url'] is! String ||
        value['pairing_token'] is! String ||
        !RegExp(
          r'^ql_pair_[A-Za-z0-9_-]{43}$',
        ).hasMatch(value['pairing_token'] as String)) {
      throw const FormatException('绑定码无效，请在网页重新生成');
    }
    return PairingCode(
      validateServer(value['server_url'] as String),
      value['pairing_token'] as String,
    );
  }
}

class DeviceSession {
  final Uri server;
  final String token, deviceId, deviceName;
  final DateTime expiresAt;
  const DeviceSession({
    required this.server,
    required this.token,
    required this.deviceId,
    required this.deviceName,
    required this.expiresAt,
  });
  factory DeviceSession.fromJson(Map<String, dynamic> json) {
    final token = json['access_token'] as String;
    if (!RegExp(r'^ql_device_[A-Za-z0-9_-]{64}$').hasMatch(token)) {
      throw const FormatException('服务器返回了无效的设备凭据');
    }
    return DeviceSession(
      server: validateServer(json['server_url'] as String),
      token: token,
      deviceId: json['device_id'] as String,
      deviceName: json['device_name'] as String,
      expiresAt: DateTime.parse(json['expires_at'] as String),
    );
  }
  Map<String, dynamic> toJson() => {
    'server_url': server.toString(),
    'access_token': token,
    'device_id': deviceId,
    'device_name': deviceName,
    'expires_at': expiresAt.toUtc().toIso8601String(),
  };
}

abstract class SessionStore {
  Future<DeviceSession?> read();
  Future<void> write(DeviceSession session);
  Future<void> clear();
}

class SecureSessionStore implements SessionStore {
  static const _key = 'qiye.device.v1';
  final FlutterSecureStorage _storage;
  SecureSessionStore([FlutterSecureStorage? storage])
    : _storage = storage ?? const FlutterSecureStorage();
  @override
  Future<DeviceSession?> read() async {
    final raw = await _storage.read(key: _key);
    if (raw == null) return null;
    try {
      final session = DeviceSession.fromJson(
        jsonDecode(raw) as Map<String, dynamic>,
      );
      if (session.expiresAt.isAfter(DateTime.now())) return session;
    } catch (_) {
      /* Invalid local credentials must never reach a server. */
    }
    await clear();
    return null;
  }

  @override
  Future<void> write(DeviceSession session) =>
      _storage.write(key: _key, value: jsonEncode(session.toJson()));
  @override
  Future<void> clear() => _storage.delete(key: _key);
}
