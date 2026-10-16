import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'package:crypto/crypto.dart';
import 'package:http/http.dart' as http;
import 'app_image_cache.dart';
import 'session.dart';

class ApiFailure implements Exception {
  final String message;
  final int? status;
  const ApiFailure(this.message, [this.status]);
  @override
  String toString() => message;
}

class LibraryApi {
  final Uri server;
  final String? _token;
  String? _adminCookie;
  final http.Client _client;
  final AppImageCache _imageCache;
  final DateTime? expiresAt;
  final DateTime Function() _clock;
  late final String _cacheNamespace = AppImageCache.namespace(
    server,
    _token ?? _adminCookie,
  );
  final void Function()? onUnauthorized;
  bool _notified = false;
  bool _closed = false;
  bool _cacheDisabled = false;
  bool get isAdministrator => _adminCookie != null;
  LibraryApi({
    required this.server,
    String? token,
    http.Client? client,
    AppImageCache? imageCache,
    this.expiresAt,
    DateTime Function()? clock,
    this.onUnauthorized,
  }) : _token = token,
       _client = client ?? http.Client(),
       _imageCache = imageCache ?? AppImageCache.shared,
       _clock = clock ?? DateTime.now;
  factory LibraryApi.forSession(
    DeviceSession session, {
    void Function()? onUnauthorized,
  }) => LibraryApi(
    server: session.server,
    token: session.token,
    expiresAt: session.expiresAt,
    onUnauthorized: onUnauthorized,
  );

  /// A separate in-memory administrator session. The reading token and secure
  /// pairing store are never promoted or replaced by administrator credentials.
  static Future<LibraryApi> administrator({
    required Uri server,
    required String password,
    http.Client? client,
    void Function()? onUnauthorized,
  }) async {
    final api = LibraryApi(
      server: validateServer(server.toString()),
      client: client,
      onUnauthorized: onUnauthorized,
    );
    try {
      final request = http.Request('POST', api.resolve('/auth/login'))
        ..followRedirects = false
        ..headers['Content-Type'] = 'application/json'
        ..body = jsonEncode({'password': password});
      final response = await api._client
          .send(request)
          .timeout(const Duration(seconds: 20));
      await response.stream.drain<void>().timeout(const Duration(seconds: 20));
      if (response.statusCode != 200) {
        throw ApiFailure(
          response.statusCode == 429 ? '登录尝试过多，请五分钟后重试' : '管理员密码错误或服务器不可用',
          response.statusCode,
        );
      }
      final cookie = RegExp(
        r'(?:^|,\s*)qiye_session=([A-Za-z0-9_.=-]+)(?:;|$)',
      ).firstMatch(response.headers['set-cookie'] ?? '')?.group(1);
      if (cookie == null || cookie.length > 1024) {
        throw const ApiFailure('服务器没有返回有效的管理员会话');
      }
      api._adminCookie = 'qiye_session=$cookie';
      return api;
    } catch (error) {
      api.close();
      if (error is ApiFailure) rethrow;
      throw const ApiFailure('管理员登录失败，请检查网络后重试');
    }
  }

  Map<String, String> _authorization() => {
    if (_token != null) 'Authorization': 'Bearer $_token',
    'Cookie': ?_adminCookie,
  };

  void _unauthorized() {
    _cacheDisabled = true;
    unawaited(clearImageCache().catchError((Object _) {}));
    if ((_token != null || _adminCookie != null) && !_notified) {
      _notified = true;
      onUnauthorized?.call();
    }
  }

  void _ensureActive() {
    if (_closed) throw const ApiFailure('服务器连接已关闭');
    if (expiresAt != null && !expiresAt!.isAfter(_clock())) {
      _unauthorized();
      throw const ApiFailure('设备授权已失效，请重新扫码绑定', 401);
    }
  }

  /// Authentication for native media requests to an address checked by resolve.
  /// Tokens stay in headers and never become part of a playable/shareable URL.
  Map<String, String> mediaHeaders() {
    _ensureActive();
    return Map.unmodifiable(_authorization());
  }

  Uri resolve(String path, [Map<String, String>? query]) {
    if (path.contains('\\') || path.contains('#') || path.startsWith('//')) {
      throw const ApiFailure('资源地址无效');
    }
    try {
      if (path
          .split('/')
          .map(Uri.decodeComponent)
          .any(
            (segment) =>
                segment == '.' ||
                segment == '..' ||
                segment.contains('/') ||
                segment.contains('\\'),
          )) {
        throw const ApiFailure('资源地址无效');
      }
    } on FormatException {
      throw const ApiFailure('资源地址无效');
    }
    final supplied = Uri.tryParse(path);
    if (supplied == null || supplied.userInfo.isNotEmpty) {
      throw const ApiFailure('资源地址无效');
    }
    final uri = supplied.hasScheme
        ? supplied
        : server.resolve(
            path.startsWith('/api/')
                ? path
                : '/api${path.startsWith('/') ? path : '/$path'}',
          );
    if (uri.origin != server.origin ||
        !uri.path.startsWith('/api/') ||
        uri.hasFragment ||
        uri.hasQuery ||
        uri.pathSegments.any(
          (s) => s == '.' || s == '..' || s.contains('/') || s.contains('\\'),
        )) {
      throw const ApiFailure('拒绝访问服务器以外的资源');
    }
    return query == null ? uri : uri.replace(queryParameters: query);
  }

  Future<Uint8List> _request(
    String method,
    String path, {
    Map<String, String>? query,
    Map<String, dynamic>? body,
    int maxBytes = 20 * 1024 * 1024,
    bool imageResponse = false,
    Duration headerTimeout = const Duration(seconds: 20),
  }) async {
    _ensureActive();
    final request = http.Request(method, resolve(path, query))
      ..followRedirects = false;
    request.headers['Accept'] = 'application/json';
    request.headers.addAll(_authorization());
    if (body != null) {
      request.headers['Content-Type'] = 'application/json';
      request.body = jsonEncode(body);
    }
    try {
      final response = await _client.send(request).timeout(headerTimeout);
      if (response.statusCode >= 300 && response.statusCode < 400) {
        await response.stream.listen((_) {}).cancel();
        throw const ApiFailure('服务器返回了重定向，请使用最终服务器地址重新绑定');
      }
      final limit = response.statusCode >= 400 ? 64 * 1024 : maxBytes;
      if ((response.contentLength ?? 0) > limit) {
        await response.stream.listen((_) {}).cancel();
        throw const ApiFailure('文件过大，请使用网页查看');
      }
      final bytes = BytesBuilder(copy: false);
      await for (final chunk in response.stream.timeout(
        const Duration(seconds: 30),
      )) {
        if (bytes.length + chunk.length > limit) {
          throw const ApiFailure('文件超过读取上限');
        }
        bytes.add(chunk);
      }
      final result = bytes.takeBytes();
      if (response.statusCode >= 400) {
        if (response.statusCode == 401) _unauthorized();
        if (response.statusCode == 403) {
          unawaited(clearImageCache().catchError((Object _) {}));
        }
        final message = switch (response.statusCode) {
          401 => isAdministrator ? '管理员登录已过期，请重新登录' : '设备授权已失效，请重新扫码绑定',
          403 => '没有此操作权限，请检查登录状态',
          404 => '内容不存在，可能已被删除',
          429 => '请求过于频繁，请稍后重试',
          >= 500 => '服务器暂时不可用，请稍后重试',
          _ => '请求未完成，请刷新后重试',
        };
        // Error bodies may contain proxy diagnostics or credentials; never display them.
        throw ApiFailure(message, response.statusCode);
      }
      if (imageResponse && response.statusCode != 200) {
        throw const ApiFailure('图片响应不完整，请重试');
      }
      return result;
    } on ApiFailure {
      rethrow;
    } on TimeoutException {
      throw const ApiFailure('连接超时，请确认手机能访问服务器');
    } catch (_) {
      throw const ApiFailure('连接失败，请检查服务器地址、网络与 HTTPS 证书');
    }
  }

  Future<dynamic> _json(
    String method,
    String path, {
    Map<String, String>? query,
    Map<String, dynamic>? body,
  }) async {
    final bytes = await _request(method, path, query: query, body: body);
    try {
      return jsonDecode(utf8.decode(bytes));
    } catch (_) {
      throw const ApiFailure('服务器响应格式不正确，请更新客户端或服务器');
    }
  }

  Future<dynamic> get(String path, {Map<String, String>? query}) =>
      _json('GET', path, query: query);
  Future<dynamic> post(String path, [Map<String, dynamic>? body]) =>
      _json('POST', path, body: body);
  Future<dynamic> put(String path, [Map<String, dynamic>? body]) =>
      _json('PUT', path, body: body);
  Future<dynamic> delete(String path) => _json('DELETE', path);
  Future<dynamic> manage(
    String method,
    String path, {
    Map<String, String>? query,
    Map<String, dynamic>? body,
  }) => _json(method, path, query: query, body: body);

  Future<Uint8List> managementDownload(
    String method,
    String path, {
    Map<String, String>? query,
  }) => _request(
    method,
    path,
    query: query,
    maxBytes: 256 * 1024 * 1024,
    headerTimeout: const Duration(minutes: 5),
  );

  Future<dynamic> managementUpload(
    String method,
    String path, {
    Map<String, String>? query,
    required List<({String field, String name, String path})> files,
    Map<String, String> fields = const {},
    bool raw = false,
  }) async {
    _ensureActive();
    http.BaseRequest request;
    if (raw) {
      final file = File(files.single.path);
      final streamed = http.StreamedRequest(method, resolve(path, query))
        ..contentLength = await file.length()
        ..headers['Content-Type'] = 'application/vnd.android.package-archive';
      Future<void> feed() async {
        try {
          await streamed.sink.addStream(file.openRead());
        } catch (error, stack) {
          try {
            streamed.sink.addError(error, stack);
          } catch (_) {
            /* Transport already closed. */
          }
        } finally {
          try {
            await streamed.sink.close();
          } catch (_) {
            /* Reported by send(). */
          }
        }
      }

      unawaited(feed());
      request = streamed;
    } else {
      final multipart = http.MultipartRequest(method, resolve(path, query))
        ..fields.addAll(fields);
      for (final file in files) {
        multipart.files.add(
          await http.MultipartFile.fromPath(
            file.field,
            file.path,
            filename: file.name,
          ),
        );
      }
      request = multipart;
    }
    request.followRedirects = false;
    request.headers.addAll(_authorization());
    try {
      final response = await _client
          .send(request)
          .timeout(const Duration(minutes: 10));
      final data = BytesBuilder(copy: false);
      await for (final chunk in response.stream.timeout(
        const Duration(minutes: 2),
      )) {
        if (data.length + chunk.length > 20 * 1024 * 1024) {
          throw const ApiFailure('响应超过大小上限');
        }
        data.add(chunk);
      }
      if (response.statusCode == 401) _unauthorized();
      if (response.statusCode < 200 || response.statusCode >= 300) {
        throw ApiFailure('上传失败，请检查文件格式、权限、服务器空间后重试', response.statusCode);
      }
      return jsonDecode(utf8.decode(data.takeBytes()));
    } on ApiFailure {
      rethrow;
    } catch (_) {
      throw const ApiFailure('上传中断，请检查连接后重试');
    }
  }

  Future<Uint8List> image(String path) async {
    // Validate before looking in cache: a hit must never bypass origin/path rules.
    final uri = resolve(path);
    _ensureActive();
    Future<Uint8List> load() =>
        _request('GET', path, maxBytes: 64 * 1024 * 1024, imageResponse: true);
    if (_cacheDisabled) return load();
    return _imageCache.get(
      namespace: _cacheNamespace,
      uri: uri,
      ttl: RegExp(r'^/api/assets/[^/]+/content$').hasMatch(uri.path)
          ? const Duration(days: 7)
          : const Duration(minutes: 15),
      load: load,
      mayStore: () =>
          !_closed &&
          !_cacheDisabled &&
          (expiresAt == null || expiresAt!.isAfter(_clock())),
    );
  }

  Future<ImageCacheStats> imageCacheStats() =>
      _imageCache.stats(_cacheNamespace);
  Future<void> clearImageCache() => _imageCache.clear(_cacheNamespace);
  Future<void> purgeImageCache() {
    _cacheDisabled = true;
    return clearImageCache();
  }

  Future<void> evictImage(String path) =>
      _imageCache.evict(_cacheNamespace, resolve(path));
  Future<void> downloadApk(
    String path,
    File file, {
    required int size,
    required String digest,
    required void Function(int) onProgress,
    bool Function()? isCancelled,
  }) async {
    if (!RegExp(
          r'^/api/app-updates/android/[a-f0-9]{64}/apk$',
        ).hasMatch(path) ||
        size <= 0 ||
        size > 256 * 1024 * 1024 ||
        !RegExp(r'^[a-f0-9]{64}$').hasMatch(digest) ||
        path != '/api/app-updates/android/$digest/apk') {
      throw const ApiFailure('更新包信息无效');
    }
    IOSink? sink;
    try {
      final request = http.Request('GET', resolve(path))
        ..followRedirects = false;
      if (_token != null) request.headers['Authorization'] = 'Bearer $_token';
      final response = await _client
          .send(request)
          .timeout(const Duration(seconds: 20));
      if (response.statusCode != 200 ||
          (response.contentLength != null && response.contentLength != size)) {
        await response.stream.listen((_) {}).cancel();
        if (response.statusCode == 401) _unauthorized();
        throw ApiFailure(
          response.statusCode == 401 ? '设备授权已失效，请重新扫码绑定' : '安装包下载失败或大小不符',
          response.statusCode,
        );
      }
      sink = file.openWrite();
      var received = 0;
      final clock = Stopwatch()..start();
      await for (final chunk in response.stream.timeout(
        const Duration(seconds: 30),
      )) {
        if (isCancelled?.call() == true) throw const ApiFailure('已取消下载');
        if (clock.elapsed > const Duration(minutes: 10)) {
          throw const ApiFailure('下载超时，请重试');
        }
        received += chunk.length;
        if (received > size) throw const ApiFailure('安装包超过声明大小');
        sink.add(chunk);
        // Flush each chunk to bound buffered data on slow phone storage.
        await sink.flush();
        onProgress(received);
      }
      await sink.close();
      sink = null;
      if (received != size ||
          (await sha256.bind(file.openRead()).first).toString() != digest) {
        throw const ApiFailure('安装包完整性校验失败，请重新下载');
      }
    } catch (e) {
      try {
        await sink?.close();
      } catch (_) {
        /* Remove partial data below. */
      }
      if (await file.exists()) await file.delete();
      if (e is ApiFailure) rethrow;
      throw const ApiFailure('下载中断，请检查网络和手机存储后重试');
    }
  }

  void close() {
    _adminCookie = null;
    _closed = true;
    _imageCache.release(_cacheNamespace);
    _client.close();
  }
}

/// One exchange per scan, even if the camera emits the same QR on many frames.
class PairingService {
  final SessionStore store;
  final LibraryApi Function(Uri server) createApi;
  final LibraryApi Function(DeviceSession session) createAuthorizedApi;
  bool _busy = false;
  PairingService(
    this.store, {
    LibraryApi Function(Uri server)? createApi,
    LibraryApi Function(DeviceSession session)? createAuthorizedApi,
  }) : createApi = createApi ?? ((uri) => LibraryApi(server: uri)),
       createAuthorizedApi =
           createAuthorizedApi ?? ((session) => LibraryApi.forSession(session));
  Future<DeviceSession> bind(PairingCode code, String deviceName) async {
    if (_busy) throw const ApiFailure('正在绑定，请稍候');
    _busy = true;
    final api = createApi(code.server);
    try {
      dynamic result;
      try {
        result = await api.post('/devices/pairing/exchange', {
          'pairing_token': code.token,
          'device_name': deviceName,
        });
      } on ApiFailure catch (e) {
        if (e.status == 400) throw const ApiFailure('绑定码已失效，请在网页重新生成后扫码', 400);
        rethrow;
      }
      final session = DeviceSession.fromJson({
        ...result as Map<String, dynamic>,
        'server_url': code.server.toString(),
      });
      if (!session.expiresAt.isAfter(DateTime.now())) {
        throw const ApiFailure('服务器返回的授权已过期');
      }
      try {
        await store.write(session);
      } catch (_) {
        final revoke = createAuthorizedApi(session);
        try {
          await revoke.post('/devices/disconnect');
        } catch (_) {
          /* Can also revoke from the web. */
        } finally {
          revoke.close();
        }
        throw const ApiFailure('无法安全保存凭据，请检查系统存储，并在网页撤销本次设备后重新绑定');
      }
      return session;
    } on ApiFailure {
      rethrow;
    } catch (_) {
      throw const ApiFailure('绑定响应无效，请重新生成二维码后重试');
    } finally {
      api.close();
      _busy = false;
    }
  }
}
