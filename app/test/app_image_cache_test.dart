import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/app_image_cache.dart';

void main() {
  late Directory folder;
  late AppImageCache cache;
  late DateTime now;
  final server = Uri.parse('https://library.test');
  const token = 'test-device-secret';
  const cover = '/api/items/book/cover';
  const page = '/api/assets/page/content';
  setUp(() async {
    folder = await Directory.systemTemp.createTemp('qiye-cache-test-');
    now = DateTime.utc(2026, 10, 15);
    cache = AppImageCache(
      directoryProvider: () async => folder,
      clock: () => now,
    );
  });
  tearDown(() async {
    await cache.stats(AppImageCache.namespace(server, token));
    await folder.delete(recursive: true);
  });

  LibraryApi api({
    AppImageCache? imageCache,
    String? credential = token,
    Uri? origin,
    DateTime? expiresAt,
    void Function()? onUnauthorized,
    required Future<http.Response> Function(http.Request) response,
  }) => LibraryApi(
    server: origin ?? server,
    token: credential,
    imageCache: imageCache ?? cache,
    expiresAt: expiresAt,
    clock: () => now,
    onUnauthorized: onUnauthorized,
    client: MockClient(response),
  );

  test('parallel loads and later widget instances use one request', () async {
    var requests = 0;
    final started = Completer<void>();
    final response = Completer<http.Response>();
    final connection = api(
      response: (_) {
        requests++;
        started.complete();
        return response.future;
      },
    );
    final first = connection.image(cover);
    final second = connection.image(cover);
    await started.future;
    response.complete(http.Response.bytes([1, 2, 3], 200));
    expect(await first, [1, 2, 3]);
    expect(await second, [1, 2, 3]);
    expect(await connection.image(cover), [1, 2, 3]);
    expect(requests, 1);
    expect((await connection.imageCacheStats()).entries, 1);
    connection.close();
  });

  test('a restarted cache reuses disk without credentials in files', () async {
    var requests = 0;
    Future<http.Response> response(http.Request _) async {
      requests++;
      return http.Response.bytes([11, 12, 13], 200);
    }

    final first = api(response: response);
    await first.image(cover);
    await first.imageCacheStats();
    first.close();
    final restarted = AppImageCache(
      directoryProvider: () async => folder,
      clock: () => now,
    );
    final second = api(imageCache: restarted, response: response);
    expect(await second.image(cover), [11, 12, 13]);
    expect(requests, 1);
    final files = await folder
        .list(recursive: true)
        .where((e) => e is File)
        .cast<File>()
        .toList();
    expect(files, hasLength(1));
    expect(files.single.path, isNot(contains(token)));
    expect(
      utf8.decode(await files.single.readAsBytes(), allowMalformed: true),
      isNot(contains(token)),
    );
    await second.imageCacheStats();
    second.close();
  });

  test(
    'origin and authorization each isolate the same resource path',
    () async {
      var requests = 0;
      Future<http.Response> response(http.Request _) async =>
          http.Response.bytes([++requests], 200);
      final a = api(response: response);
      final b = api(credential: 'second-secret', response: response);
      final c = api(
        origin: Uri.parse('https://another.test'),
        response: response,
      );
      expect(await a.image(cover), [1]);
      expect(await b.image(cover), [2]);
      expect(await c.image(cover), [3]);
      expect(await a.image(cover), [1]);
      await a.clearImageCache();
      expect(await b.image(cover), [2]);
      expect(await c.image(cover), [3]);
      expect(requests, 3);
      await c.imageCacheStats();
      for (final connection in [a, b, c]) {
        connection.close();
      }
    },
  );

  test(
    'cover refreshes after 15 minutes while pages have a longer TTL',
    () async {
      var requests = 0;
      final connection = api(
        response: (_) async => http.Response.bytes([++requests], 200),
      );
      expect(await connection.image(cover), [1]);
      expect(await connection.image(page), [2]);
      now = now.add(const Duration(minutes: 14));
      expect(await connection.image(cover), [1]);
      now = now.add(const Duration(minutes: 2));
      expect(await connection.image(cover), [3]);
      expect(await connection.image(page), [2]);
      await connection.imageCacheStats();
      connection.close();
      now = now.add(const Duration(days: 8));
      final restarted = AppImageCache(
        directoryProvider: () async => folder,
        clock: () => now,
      );
      final next = api(
        imageCache: restarted,
        response: (_) async => http.Response.bytes([++requests], 200),
      );
      expect(await next.image(page), [4]);
      await next.imageCacheStats();
      next.close();
    },
  );

  test('memory LRU remains bounded when disk is unavailable', () async {
    cache = AppImageCache(
      directoryProvider: () async =>
          throw const FileSystemException('unavailable'),
      maxMemoryBytes: 8,
    );
    var requests = 0;
    final connection = api(
      response: (_) async {
        requests++;
        return http.Response.bytes([1, 2, 3, 4], 200);
      },
    );
    await connection.image('/items/a/cover');
    await connection.image('/items/b/cover');
    await connection.image('/items/a/cover');
    await connection.image('/items/c/cover');
    await connection.image('/items/b/cover');
    expect(requests, 4);
    expect((await connection.imageCacheStats()).memoryBytes, 8);
    connection.close();
  });

  test(
    'oversized pending writes are skipped without losing the displayed image',
    () async {
      cache = AppImageCache(
        directoryProvider: () async => folder,
        maxPendingWriteBytes: 2,
      );
      final connection = api(
        response: (_) async => http.Response.bytes([1, 2, 3], 200),
      );
      expect(await connection.image(cover), [1, 2, 3]);
      final stats = await connection.imageCacheStats();
      expect(stats.entries, 0);
      expect(stats.memoryBytes, 3);
      connection.close();
    },
  );

  test(
    'disk LRU honors touches and includes record overhead in capacity',
    () async {
      cache = AppImageCache(
        directoryProvider: () async => folder,
        clock: () => now,
        maxMemoryBytes: 4,
        maxDiskBytes: 40,
      );
      final fetched = <String>[];
      Future<http.Response> response(http.Request request) async {
        fetched.add(request.url.path);
        return http.Response.bytes([1, 2, 3, 4], 200);
      }

      final connection = api(response: response);
      await connection.image('/items/a/cover');
      now = now.add(const Duration(seconds: 1));
      await connection.image('/items/b/cover');
      now = now.add(const Duration(seconds: 1));
      await connection.image('/items/a/cover');
      now = now.add(const Duration(seconds: 1));
      await connection.image('/items/c/cover');
      final stats = await connection.imageCacheStats();
      expect(stats.bytes, 40);
      expect(stats.entries, 2);
      connection.close();
      final restarted = AppImageCache(
        directoryProvider: () async => folder,
        clock: () => now,
        maxMemoryBytes: 4,
        maxDiskBytes: 40,
      );
      final next = api(imageCache: restarted, response: response);
      await next.image('/items/a/cover');
      await next.image('/items/c/cover');
      expect(fetched, hasLength(3));
      await next.image('/items/b/cover');
      expect(fetched, hasLength(4));
      await next.imageCacheStats();
      next.close();
    },
  );

  test(
    'clear while a request is pending prevents late memory or disk writes',
    () async {
      final started = Completer<void>();
      final response = Completer<http.Response>();
      var requests = 0;
      final connection = api(
        response: (_) {
          requests++;
          if (requests == 1) {
            started.complete();
            return response.future;
          }
          return Future.value(http.Response.bytes([2], 200));
        },
      );
      final pending = connection.image(cover);
      await started.future;
      await connection.clearImageCache();
      response.complete(http.Response.bytes([1], 200));
      expect(await pending, [1]);
      final empty = await connection.imageCacheStats();
      expect(empty.bytes, 0);
      expect(empty.memoryBytes, 0);
      expect(await connection.image(cover), [2]);
      expect(requests, 2);
      await connection.imageCacheStats();
      connection.close();
    },
  );

  test('evict during a load cannot remove a newer retry result', () async {
    final started = Completer<void>();
    final response = Completer<http.Response>();
    var requests = 0;
    final connection = api(
      response: (_) {
        requests++;
        if (requests == 1) {
          started.complete();
          return response.future;
        }
        return Future.value(http.Response.bytes([2], 200));
      },
    );
    final pending = connection.image(cover);
    await started.future;
    await connection.evictImage(cover);
    expect(await connection.image(cover), [2]);
    response.complete(http.Response.bytes([1], 200));
    await pending;
    expect(await connection.image(cover), [2]);
    expect(requests, 2);
    await connection.imageCacheStats();
    connection.close();
  });

  test(
    'close prevents pending writes and purge disables future cache reuse',
    () async {
      final started = Completer<void>();
      final response = Completer<http.Response>();
      final first = api(
        response: (_) {
          started.complete();
          return response.future;
        },
      );
      final pending = first.image(cover);
      await started.future;
      first.close();
      response.complete(http.Response.bytes([1], 200));
      await pending;
      expect((await first.imageCacheStats()).entries, 0);
      var requests = 0;
      final second = api(
        response: (_) async => http.Response.bytes([++requests], 200),
      );
      await second.image(cover);
      await second.purgeImageCache();
      await second.image(cover);
      expect((await second.imageCacheStats()).entries, 0);
      expect((await second.imageCacheStats()).memoryBytes, 0);
      expect(requests, 2);
      second.close();
    },
  );

  test(
    'failed and redirect responses are never cached and remain retryable',
    () async {
      for (final status in [401, 403, 404, 500, 302, 206]) {
        var requests = 0;
        final connection = api(
          credential: 'status-$status',
          response: (_) async {
            requests++;
            return requests == 1
                ? http.Response('error', status)
                : http.Response.bytes([9], 200);
          },
        );
        await expectLater(connection.image(cover), throwsA(isA<ApiFailure>()));
        expect((await connection.imageCacheStats()).entries, 0);
        expect(await connection.image(cover), [9]);
        expect(requests, 2);
        await connection.imageCacheStats();
        connection.close();
      }
    },
  );

  test(
    'path validation happens before a cached response can be returned',
    () async {
      var requests = 0;
      final connection = api(
        response: (_) async => http.Response.bytes([++requests], 200),
      );
      await connection.image(cover);
      for (final invalid in [
        'https://other.test$cover',
        '$cover?secret=x',
        '/api/items/book/../cover',
        '/api/items/book/%2fcover',
      ]) {
        await expectLater(
          connection.image(invalid),
          throwsA(isA<ApiFailure>()),
        );
      }
      expect(requests, 1);
      await connection.imageCacheStats();
      connection.close();
    },
  );

  test('session expiry and server 401 invalidate cached image bytes', () async {
    var invalidations = 0;
    final connection = api(
      expiresAt: now.add(const Duration(minutes: 1)),
      onUnauthorized: () => invalidations++,
      response: (_) async => http.Response.bytes([1], 200),
    );
    await connection.image(cover);
    now = now.add(const Duration(minutes: 2));
    await expectLater(
      connection.image(cover),
      throwsA(isA<ApiFailure>().having((e) => e.status, 'status', 401)),
    );
    expect((await connection.imageCacheStats()).entries, 0);
    expect(invalidations, 1);
    connection.close();
    final second = api(
      response: (request) async => request.url.path == cover
          ? http.Response.bytes([2], 200)
          : http.Response('', 401),
    );
    await second.image(cover);
    await expectLater(second.get('/items'), throwsA(isA<ApiFailure>()));
    final stats = await second.imageCacheStats();
    expect(stats.entries, 0);
    expect(stats.memoryBytes, 0);
    second.close();
  });

  test(
    'bad disk records refetch and abandoned temporary records are removed',
    () async {
      var requests = 0;
      Future<http.Response> response(http.Request _) async =>
          http.Response.bytes([++requests], 200);
      final connection = api(response: response);
      await connection.image(cover);
      await connection.imageCacheStats();
      connection.close();
      final file =
          (await folder
                  .list(recursive: true)
                  .where((e) => e is File)
                  .cast<File>()
                  .toList())
              .single;
      await file.writeAsBytes([0, 1, 2]);
      final abandoned = File('${file.path}.tmp');
      await abandoned.writeAsBytes([9, 9, 9]);
      final restarted = AppImageCache(
        directoryProvider: () async => folder,
        clock: () => now,
      );
      final second = api(imageCache: restarted, response: response);
      expect(await second.image(cover), [2]);
      await second.imageCacheStats();
      expect(await abandoned.exists(), isFalse);
      second.close();
    },
  );

  test(
    'invalid or unwritable disk location falls back to network and memory',
    () async {
      final file = File.fromUri(folder.uri.resolve('not-a-directory'));
      await file.writeAsString('test');
      cache = AppImageCache(
        directoryProvider: () async => Directory(file.path),
      );
      var requests = 0;
      final connection = api(
        response: (_) async => http.Response.bytes([++requests], 200),
      );
      expect(await connection.image(cover), [1]);
      expect(await connection.image(cover), [1]);
      expect((await connection.imageCacheStats()).entries, 0);
      await expectLater(
        connection.clearImageCache(),
        throwsA(isA<FileSystemException>()),
      );
      connection.close();
    },
  );
}
