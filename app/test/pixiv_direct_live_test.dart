import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';
import 'package:qiye_library/pixiv_direct_api.dart';
import 'package:qiye_library/app_image_cache.dart';

class _Store implements PixivTokenStore {
  Map<String, dynamic>? value;
  @override
  Future<Map<String, dynamic>?> read() async => value;
  @override
  Future<void> write(Map<String, dynamic> data) async {
    value = data;
  }

  @override
  Future<void> clear() async {
    value = null;
  }
}

class _RealHttp extends HttpOverrides {}

class _TraceClient extends http.BaseClient {
  final _inner = IOClient(HttpClient()..findProxy = (_) => 'DIRECT');
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    final response = await _inner.send(request);
    // No query strings, headers, response content or account identifiers.
    stdout.writeln(
      'DIRECT ${request.url.host}${request.url.path} ${response.statusCode}',
    );
    return response;
  }

  @override
  void close() => _inner.close();
}

void main() {
  final token = Platform.environment['QIYE_PIXIV_LIVE_TOKEN'];
  test(
    'optional official Pixiv direct protocol smoke',
    () async {
      await HttpOverrides.runWithHttpOverrides(() async {
        final store = _Store();
        final directory = await Directory.systemTemp.createTemp(
          'qiye-pixiv-smoke-',
        );
        final cache = AppImageCache(directoryProvider: () async => directory);
        final api = PixivDirectApi(
          store: store,
          scope: 'live-protocol-verification',
          client: _TraceClient(),
          cache: cache,
        );
        try {
          await api.connect(token!);
          expect(api.userId, isNotEmpty);
          var total = 0;
          for (final category in ['illustration', 'manga', 'novel']) {
            for (final restrict in ['public', 'private']) {
              final page = await api.page(
                mode: 'bookmarks',
                category: category,
                restrict: restrict,
              );
              total += page.works.length;
              if (page.next != null) {
                await api.page(
                  mode: 'bookmarks',
                  category: category,
                  restrict: restrict,
                  next: page.next,
                );
              }
              if (page.works.isNotEmpty) {
                final work = await api.detail(
                  '${page.works.first['id']}',
                  novel: category == 'novel',
                );
                if (category == 'novel') {
                  expect(await api.novelText('${work['id']}'), isNotEmpty);
                } else {
                  expect(PixivDirectApi.pages(work), isNotEmpty);
                  final image = work['image_urls']['medium'] as String;
                  final bytes = await api.image(image);
                  expect(bytes, isNotEmpty);
                  expect(await api.image(image), bytes);
                }
              }
            }
          }
          await api.page(mode: 'following', category: 'illustration');
          await api.page(
            mode: 'search',
            category: 'illustration',
            query: 'landscape',
          );
          expect(total, greaterThan(0));
          expect(store.value?['refresh_token'], isNotEmpty);
        } finally {
          await api.logout();
          api.close();
          await directory.delete(recursive: true);
        }
      }, _RealHttp());
    },
    skip: token == null ? 'Opt-in live credentials are not present' : false,
    timeout: const Timeout(Duration(minutes: 3)),
  );
}
