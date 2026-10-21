import 'dart:async';
import 'novel_blocks.dart';
import 'dart:convert';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;

import 'api.dart';
import 'app_image_cache.dart';

abstract class PixivTokenStore {
  Future<Map<String, dynamic>?> read();
  Future<void> write(Map<String, dynamic> value);
  Future<void> clear();
}

class SecurePixivTokenStore implements PixivTokenStore {
  static final Map<String, Future<void>> _pending = {};
  final FlutterSecureStorage storage;
  final String key;
  SecurePixivTokenStore(
    Uri server, {
    this.storage = const FlutterSecureStorage(),
  }) : key =
           'qiye.pixiv.direct.v1.${sha256.convert(utf8.encode(server.origin))}';
  @override
  Future<Map<String, dynamic>?> read() async {
    await (_pending[key] ?? Future<void>.value());
    final value = await storage.read(key: key);
    if (value == null) return null;
    try {
      return jsonDecode(value) as Map<String, dynamic>;
    } catch (_) {
      await clear();
      return null;
    }
  }

  @override
  Future<void> write(Map<String, dynamic> value) =>
      _serial(() => storage.write(key: key, value: jsonEncode(value)));
  @override
  Future<void> clear() => _serial(() => storage.delete(key: key));
  Future<void> _serial(Future<void> Function() action) {
    final future = (_pending[key] ?? Future<void>.value())
        .catchError((Object _) {})
        .then((_) => action());
    _pending[key] = future.catchError((Object _) {});
    return future;
  }
}

class PixivPage {
  final List<Map<String, dynamic>> works;
  final String? next;
  const PixivPage(this.works, this.next);
}

/// All content requests go from this device to official Pixiv/CDN hosts.
/// The library server is used only for the explicit administrator import.
class PixivDirectApi {
  static const apiOrigin = 'https://app-api.pixiv.net';
  static const _oauth = 'https://oauth.secure.pixiv.net/auth/token';
  static const _clientId = 'MOBrBDS8blbauoSck0ZfDbtuzpyT';
  static const _clientSecret = 'lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj';
  static const _hashSecret =
      '28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c';
  static const _headers = {
    'User-Agent': 'PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)',
    'App-OS': 'ios',
    'App-OS-Version': '14.6',
    'Accept-Language': 'zh-cn',
  };
  final PixivTokenStore store;
  final http.Client _client;
  final AppImageCache _cache;
  final DateTime Function() _clock;
  final String scope;
  String? _refresh, _access;
  String userId = '', userName = '';
  DateTime? _expires;
  Future<void>? _authFlight;
  bool _closed = false;
  int _generation = 0;
  bool get connected => _refresh != null;
  String get _namespace =>
      AppImageCache.namespace(Uri.parse(apiOrigin), 'pixiv:$scope:$userId');

  PixivDirectApi({
    required this.store,
    required this.scope,
    http.Client? client,
    AppImageCache? cache,
    DateTime Function()? clock,
  }) : _client = client ?? http.Client(),
       _cache = cache ?? AppImageCache.shared,
       _clock = clock ?? DateTime.now;

  static bool validToken(Object? token) =>
      token is String && RegExp(r'^[A-Za-z0-9_.-]{10,4096}$').hasMatch(token);
  static String id(Object? value) {
    final text = value?.toString() ?? '';
    if (!RegExp(r'^[1-9]\d{0,19}$').hasMatch(text)) {
      throw const ApiFailure('Pixiv 编号无效');
    }
    return text;
  }

  Future<void> restore() async {
    final generation = _generation;
    final data = await store.read();
    if (_closed || generation != _generation || data == null) return;
    if (!validToken(data['refresh_token']) ||
        !RegExp(r'^[1-9]\d{0,19}$').hasMatch('${data['user_id']}')) {
      await store.clear();
      return;
    }
    _refresh = data['refresh_token'] as String;
    userId = '${data['user_id']}';
    userName = data['user_name'] as String? ?? '';
  }

  Future<Uint8List> _send(
    http.Request request, {
    int limit = 8 * 1024 * 1024,
  }) async {
    if (_closed) throw const ApiFailure('Pixiv 连接已关闭');
    request.followRedirects = false;
    try {
      final response = await _client
          .send(request)
          .timeout(const Duration(seconds: 30));
      if (_closed) {
        await response.stream.listen((_) {}).cancel();
        throw const ApiFailure('Pixiv 连接已关闭');
      }
      if (response.statusCode != 200) {
        await response.stream.listen((_) {}).cancel();
        throw ApiFailure(switch (response.statusCode) {
          401 => 'Pixiv 授权已失效，请重新导入主账号令牌',
          403 || 404 => 'Pixiv 拒绝访问或作品不可见，请检查主账号权限',
          429 => 'Pixiv 请求过于频繁，请稍后重试',
          >= 300 && < 400 => 'Pixiv 返回了重定向，已停止请求',
          _ => 'Pixiv 暂时不可用，请稍后重试',
        }, response.statusCode);
      }
      if ((response.contentLength ?? 0) > limit) {
        await response.stream.listen((_) {}).cancel();
        throw const ApiFailure('Pixiv 内容超过读取上限');
      }
      final bytes = BytesBuilder(copy: false);
      await for (final chunk in response.stream.timeout(
        const Duration(seconds: 20),
      )) {
        if (bytes.length + chunk.length > limit) {
          throw const ApiFailure('Pixiv 内容超过读取上限');
        }
        bytes.add(chunk);
      }
      if (_closed) throw const ApiFailure('Pixiv 连接已关闭');
      return bytes.takeBytes();
    } on ApiFailure {
      rethrow;
    } on TimeoutException {
      throw const ApiFailure('Pixiv 直连超时，请确认手机网络能够访问 Pixiv');
    } catch (_) {
      throw const ApiFailure('Pixiv 直连失败，请检查手机网络与 HTTPS 证书');
    }
  }

  Map<String, dynamic> _json(Uint8List bytes) {
    try {
      return jsonDecode(utf8.decode(bytes)) as Map<String, dynamic>;
    } catch (_) {
      throw const ApiFailure('Pixiv 响应格式已变化，请更新 App');
    }
  }

  Future<void> connect(String refresh, {String? expectedUser}) async {
    if (!validToken(refresh)) throw const ApiFailure('主账号令牌格式无效');
    final generation = ++_generation;
    final now = _clock().toUtc();
    final stamp = '${now.toIso8601String().split('.').first}+00:00';
    final request = http.Request('POST', Uri.parse(_oauth))
      ..headers.addAll({
        ..._headers,
        'X-Client-Time': stamp,
        'X-Client-Hash': md5
            .convert(utf8.encode(stamp + _hashSecret))
            .toString(),
      })
      ..bodyFields = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh,
        'client_id': _clientId,
        'client_secret': _clientSecret,
        'get_secure_url': '1',
      };
    final raw = _json(await _send(request));
    final data = raw['response'] is Map
        ? Map<String, dynamic>.from(raw['response'] as Map)
        : raw;
    final user = data['user'];
    if (user is! Map || !validToken(data['access_token'])) {
      throw const ApiFailure('Pixiv 没有返回有效登录信息');
    }
    final actualId = id(user['id']);
    if (expectedUser != null && actualId != expectedUser) {
      throw const ApiFailure('Pixiv 账号身份发生变化，请重新导入主账号令牌');
    }
    final expires = data['expires_in'];
    if (expires is! num || expires <= 0 || expires > 86400) {
      throw const ApiFailure('Pixiv 授权有效期无效');
    }
    if (_closed || generation != _generation) {
      throw const ApiFailure('Pixiv 登录已取消');
    }
    final nextRefresh = validToken(data['refresh_token'])
        ? data['refresh_token'] as String
        : refresh;
    await store.write({
      'refresh_token': nextRefresh,
      'user_id': actualId,
      'user_name': '${user['name'] ?? ''}',
    });
    if (_closed || generation != _generation) {
      throw const ApiFailure('Pixiv 登录已取消');
    }
    _refresh = nextRefresh;
    _access = data['access_token'] as String;
    userId = actualId;
    userName = '${user['name'] ?? ''}';
    _expires = now.add(Duration(seconds: expires.toInt()));
  }

  Future<void> _authorize() async {
    if (_closed || _refresh == null) throw const ApiFailure('请先导入主账号令牌');
    if (_access != null &&
        _expires!.isAfter(_clock().add(const Duration(seconds: 60)))) {
      return;
    }
    if (_authFlight != null) return _authFlight!;
    final future = connect(
      _refresh!,
      expectedUser: userId.isEmpty ? null : userId,
    );
    _authFlight = future;
    try {
      await future;
    } finally {
      if (identical(_authFlight, future)) _authFlight = null;
    }
  }

  Uri _apiUri(String path, [Map<String, String>? query]) {
    final uri = Uri.parse(apiOrigin).resolve(path);
    if (uri.origin != apiOrigin ||
        uri.userInfo.isNotEmpty ||
        uri.hasFragment ||
        uri.hasQuery ||
        !const {
          '/v1/user/bookmarks/illust',
          '/v1/user/bookmarks/novel',
          '/v1/user/illusts',
          '/v1/user/novels',
          '/v2/illust/follow',
          '/v1/novel/follow',
          '/v1/search/illust',
          '/v1/search/novel',
          '/v1/illust/detail',
          '/v2/novel/detail',
          '/webview/v2/novel',
        }.contains(uri.path) ||
        path.contains('\\') ||
        path.contains('%') ||
        path.contains('..')) {
      throw const ApiFailure('拒绝访问 Pixiv 以外的接口');
    }
    return query == null ? uri : uri.replace(queryParameters: query);
  }

  Future<Uint8List> _apiBytes(Uri uri, {bool retry = true}) async {
    await _authorize();
    final request = http.Request('GET', uri)
      ..headers.addAll({..._headers, 'Authorization': 'Bearer $_access'});
    try {
      return await _send(request);
    } on ApiFailure catch (error) {
      if (error.status == 401 && retry) {
        _access = null;
        return _apiBytes(uri, retry: false);
      }
      rethrow;
    }
  }

  Future<PixivPage> page({
    required String mode,
    required String category,
    String restrict = 'public',
    String query = '',
    String? authorId,
    String? next,
  }) async {
    if (!['illustration', 'manga', 'novel'].contains(category) ||
        !['public', 'private'].contains(restrict)) {
      throw const ApiFailure('Pixiv 分类无效');
    }
    await _authorize();
    final novel = category == 'novel';
    late String path;
    late Map<String, String> parameters;
    if (mode == 'bookmarks') {
      path = '/v1/user/bookmarks/${novel ? 'novel' : 'illust'}';
      parameters = {'user_id': userId, 'restrict': restrict};
    } else if (mode == 'following') {
      path = novel ? '/v1/novel/follow' : '/v2/illust/follow';
      parameters = {'restrict': 'public'};
    } else if (mode == 'author' && authorId != null) {
      path = novel ? '/v1/user/novels' : '/v1/user/illusts';
      parameters = {
        'user_id': id(authorId),
        if (!novel) 'type': category == 'manga' ? 'manga' : 'illust',
      };
    } else if (mode == 'search' &&
        query.trim().isNotEmpty &&
        query.length <= 200) {
      path = '/v1/search/${novel ? 'novel' : 'illust'}';
      parameters = {
        'word': query.trim(),
        'search_target': novel ? 'keyword' : 'title_and_caption',
        'sort': 'date_desc',
      };
    } else {
      throw const ApiFailure('请输入搜索词或选择有效目录');
    }
    Uri uri = _apiUri(path, parameters);
    if (next != null) {
      final cursor = Uri.tryParse(next);
      if (cursor == null ||
          cursor.origin != apiOrigin ||
          cursor.path != path ||
          cursor.hasFragment ||
          cursor.userInfo.isNotEmpty ||
          cursor.queryParametersAll.values.any(
            (values) => values.length != 1,
          ) ||
          cursor.queryParameters.keys.any(
            (key) => !{
              ...parameters.keys,
              'filter',
              'offset',
              'max_bookmark_id',
              'max_id',
              'include_translated_tag_results',
              'merge_plain_keyword_results',
            }.contains(key),
          ) ||
          parameters.entries.any(
            (entry) => cursor.queryParameters[entry.key] != entry.value,
          )) {
        throw const ApiFailure('Pixiv 分页地址越界，已停止加载');
      }
      uri = cursor;
    }
    final data = _json(await _apiBytes(uri));
    if (data['error'] != null) throw const ApiFailure('Pixiv 无法读取此目录，请检查账号权限');
    final entries = data[novel ? 'novels' : 'illusts'];
    if (entries is! List || entries.length > 500) {
      throw const ApiFailure('Pixiv 目录格式无效');
    }
    final works = <Map<String, dynamic>>[];
    for (final value in entries) {
      if (value is! Map ||
          value['visible'] == false ||
          '${value['title'] ?? ''}'.isEmpty) {
        continue;
      }
      final work = Map<String, dynamic>.from(value);
      id(work['id']);
      work['_novel'] = novel;
      if (novel ||
          (category == 'manga'
              ? work['type'] == 'manga'
              : work['type'] == 'illust')) {
        works.add(work);
      }
    }
    final nextUrl = data['next_url'];
    if (nextUrl != null && nextUrl is! String) {
      throw const ApiFailure('Pixiv 分页格式无效');
    }
    if (nextUrl == uri.toString()) throw const ApiFailure('Pixiv 分页重复，已停止加载');
    return PixivPage(works, nextUrl as String?);
  }

  Future<Map<String, dynamic>> detail(
    String remoteId, {
    required bool novel,
  }) async {
    final data = _json(
      await _apiBytes(
        _apiUri(novel ? '/v2/novel/detail' : '/v1/illust/detail', {
          novel ? 'novel_id' : 'illust_id': id(remoteId),
        }),
      ),
    );
    final value = data[novel ? 'novel' : 'illust'];
    if (value is! Map ||
        value['visible'] == false ||
        '${value['title'] ?? ''}'.isEmpty) {
      throw const ApiFailure('作品对主账号不可见，可能已删除或受访问权限限制');
    }
    final work = Map<String, dynamic>.from(value);
    if (id(work['id']) != remoteId) throw const ApiFailure('Pixiv 返回了不匹配的作品');
    work['_novel'] = novel;
    return work;
  }

  static Uri imageUri(String url) {
    final uri = Uri.tryParse(url);
    if (uri == null ||
        uri.scheme != 'https' ||
        uri.port != 443 ||
        uri.userInfo.isNotEmpty ||
        uri.hasFragment ||
        !(uri.host == 'pximg.net' || uri.host.endsWith('.pximg.net')) ||
        url.contains('\\')) {
      throw const ApiFailure('Pixiv 图片地址无效');
    }
    return uri;
  }

  Future<Uint8List> image(String url) {
    if (_closed || !connected) throw const ApiFailure('请先导入主账号令牌');
    final uri = imageUri(url);
    final generation = _generation;
    return _cache.get(
      namespace: _namespace,
      uri: uri,
      ttl: const Duration(days: 7),
      load: () => _send(
        http.Request('GET', uri)
          ..headers.addAll({..._headers, 'Referer': 'https://www.pixiv.net/'}),
        limit: 64 * 1024 * 1024,
      ),
      mayStore: () => !_closed && generation == _generation && connected,
    );
  }

  Future<void> evictImage(String url) =>
      _cache.evict(_namespace, imageUri(url));

  static List<String> pages(Map<String, dynamic> work) {
    final meta = work['meta_pages'];
    final values = <String>[];
    if (meta is List && meta.isNotEmpty) {
      for (final page in meta) {
        final images = page is Map ? page['image_urls'] : null;
        if (images is! Map || images['original'] is! String) {
          throw const ApiFailure('Pixiv 分 P 图片目录不完整');
        }
        values.add(images['original'] as String);
      }
    } else {
      final single = work['meta_single_page'];
      if (single is Map && single['original_image_url'] is String) {
        values.add(single['original_image_url'] as String);
      }
    }
    if (values.isEmpty ||
        values.length > 2000 ||
        values.length != work['page_count'] ||
        values.toSet().length != values.length) {
      throw const ApiFailure('Pixiv 分 P 图片目录不完整');
    }
    for (final url in values) {
      imageUri(url);
    }
    return values;
  }

  Future<String> novelText(String remoteId) async =>
      (await novelDocument(remoteId)).text;

  Future<NovelDocument> novelDocument(String remoteId) async {
    final html = utf8.decode(
      await _apiBytes(
        _apiUri('/webview/v2/novel', {
          'id': id(remoteId),
          'viewer_version': '20221031_ai',
        }),
      ),
    );
    final marker = RegExp(r'novel\s*:\s*\{').firstMatch(html);
    if (marker == null) throw const ApiFailure('Pixiv 小说正文格式已变化，请更新 App');
    final start = marker.end - 1;
    var depth = 0, quoted = false, escaped = false;
    for (var i = start; i < html.length; i++) {
      final char = html[i];
      if (quoted) {
        if (escaped) {
          escaped = false;
        } else if (char == r'\') {
          escaped = true;
        } else if (char == '"') {
          quoted = false;
        }
      } else if (char == '"') {
        quoted = true;
      } else if (char == '{') {
        depth++;
      } else if (char == '}' && --depth == 0) {
        try {
          final body = jsonDecode(html.substring(start, i + 1)) as Map;
          if (body['id'] != null && id(body['id']) != remoteId) {
            throw const FormatException();
          }
          final text = body['text'];
          if (text is! String || text.isEmpty) throw const FormatException();
          return await _novelDocument(text, body);
        } catch (_) {
          throw const ApiFailure('Pixiv 小说正文格式无效');
        }
      }
    }
    throw const ApiFailure('Pixiv 小说正文不完整');
  }

  Future<NovelDocument> _novelDocument(String text, Map body) async {
    final keys = novelImageMarker.allMatches(text).map((m) => m[1]!).toSet();
    if (keys.length > 500) throw const ApiFailure('小说插图超过 500 张限制');
    final images = <String, String>{};
    final artworks = <String, List<String>>{};
    for (final key in keys) {
      try {
        String? url;
        if (key.startsWith('uploadedimage:')) {
          final entry = (body['images'] as Map?)?[key.split(':')[1]];
          if (entry is Map) {
            url = ((entry['urls'] as Map?)?['original']) as String?;
          }
        } else {
          final values = key.split(':')[1].split('-');
          final ident = values[0],
              page = int.parse(values.length > 1 ? values[1] : '1') - 1;
          final urls = artworks[ident] ??= pages(
            await detail(ident, novel: false),
          );
          if (page < urls.length) url = urls[page];
        }
        if (url != null) {
          imageUri(url);
          images[key] = url;
        }
      } catch (_) {
        // Deleted or restricted illustrations must not hide the readable text.
      }
    }
    return NovelDocument(text, novelBlocks(text, images));
  }

  Future<void> logout() async {
    _generation++;
    final namespace = _namespace;
    _refresh = null;
    _access = null;
    _expires = null;
    await store.clear();
    try {
      await _cache.clear(namespace);
    } catch (_) {
      // Memory is invalidated synchronously; optional disk-cache failure must
      // not prevent credential removal or leave the UI signed in.
    }
    userId = '';
    userName = '';
  }

  void close() {
    _closed = true;
    _generation++;
    _client.close();
  }
}
