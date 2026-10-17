import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/app_image_cache.dart';
import 'package:qiye_library/pixiv_direct_api.dart';
import 'package:qiye_library/pixiv_direct_screen.dart';

class TokenStore implements PixivTokenStore {
  Map<String, dynamic>? value;
  int writes = 0;
  @override
  Future<Map<String, dynamic>?> read() async => value;
  @override
  Future<void> write(Map<String, dynamic> data) async {
    writes++;
    value = data;
  }

  @override
  Future<void> clear() async {
    value = null;
  }
}

http.Response jsonResponse(Object value, [int status = 200]) => http.Response(
  jsonEncode(value),
  status,
  headers: {'content-type': 'application/json'},
);
http.Response login([String user = '42']) => jsonResponse({
  'response': {
    'access_token': 'access-synthetic-token',
    'refresh_token': 'refresh-synthetic-token',
    'expires_in': 3600,
    'user': {'id': user, 'name': 'Synthetic account'},
  },
});
Map<String, dynamic> work(int id, {String type = 'illust'}) => {
  'id': id,
  'title': 'Synthetic $id',
  'type': type,
  'page_count': 1,
  'visible': true,
  'user': {'id': 8, 'name': 'Author'},
  'tags': [],
  'meta_single_page': {'original_image_url': 'https://i.pximg.net/$id.png'},
};

void main() {
  test(
    'direct login stores only refresh identity and sends requests to official hosts',
    () async {
      final store = TokenStore();
      final seen = <http.Request>[];
      final api = PixivDirectApi(
        store: store,
        scope: 'library.test',
        client: MockClient((request) async {
          seen.add(request);
          expect(request.followRedirects, isFalse);
          if (request.url.host == 'oauth.secure.pixiv.net') {
            expect(request.bodyFields['grant_type'], 'refresh_token');
            expect(
              request.bodyFields['refresh_token'],
              'refresh-initial-token',
            );
            expect(request.headers['x-client-hash'], isNotEmpty);
            return login();
          }
          expect(request.url.origin, PixivDirectApi.apiOrigin);
          expect(
            request.headers['Authorization'],
            'Bearer access-synthetic-token',
          );
          expect(request.url.queryParameters['user_id'], '42');
          return jsonResponse({
            'illusts': [work(1), work(2, type: 'manga')],
            'next_url': null,
          });
        }),
      );
      addTearDown(api.close);
      await api.connect('refresh-initial-token');
      final result = await api.page(mode: 'bookmarks', category: 'manga');
      expect(result.works.single['id'], 2);
      expect(api.userId, '42');
      expect(store.value!['refresh_token'], 'refresh-synthetic-token');
      expect(store.value!.containsKey('access_token'), isFalse);
      expect(seen.length, 2);
    },
  );

  for (final next in [
    'https://evil.test/v1/user/bookmarks/illust?user_id=42&restrict=public',
    'http://app-api.pixiv.net/v1/user/bookmarks/illust?user_id=42&restrict=public',
    'https://app-api.pixiv.net/v1/user/bookmarks/illust?user_id=99&restrict=public',
    'https://app-api.pixiv.net/v1/user/bookmarks/illust?user_id=42&user_id=99&restrict=public',
    'https://app-api.pixiv.net/v1/user/bookmarks/illust?user_id=42&restrict=private',
    'https://app-api.pixiv.net/v1/illust/detail?user_id=42&restrict=public',
  ]) {
    test('untrusted cursor is rejected before sending $next', () async {
      var calls = 0;
      final api = PixivDirectApi(
        store: TokenStore(),
        scope: 'test',
        client: MockClient((request) async {
          calls++;
          return login();
        }),
      );
      addTearDown(api.close);
      await api.connect('refresh-initial-token');
      await expectLater(
        api.page(mode: 'bookmarks', category: 'illustration', next: next),
        throwsA(isA<ApiFailure>()),
      );
      expect(calls, 1);
    });
  }

  test('redirects never receive a second request or a leaked token', () async {
    var calls = 0;
    final store = TokenStore();
    final api = PixivDirectApi(
      store: store,
      scope: 'test',
      client: MockClient((request) async {
        calls++;
        return http.Response(
          'private diagnostic',
          302,
          headers: {'location': 'https://evil.test'},
        );
      }),
    );
    addTearDown(api.close);
    await expectLater(
      api.connect('refresh-initial-token'),
      throwsA(isA<ApiFailure>()),
    );
    expect(calls, 1);
    expect(store.value, isNull);
  });

  test(
    'image requests merge in cache and never contain Pixiv bearer credentials',
    () async {
      final directory = await Directory.systemTemp.createTemp(
        'pixiv-direct-cache-',
      );
      addTearDown(() => directory.delete(recursive: true));
      final cache = AppImageCache(directoryProvider: () async => directory);
      var images = 0;
      final store = TokenStore();
      final api = PixivDirectApi(
        store: store,
        scope: 'test',
        cache: cache,
        client: MockClient((request) async {
          if (request.url.host == 'oauth.secure.pixiv.net') return login();
          expect(request.url.host, 'i.pximg.net');
          expect(
            request.headers.keys.map((s) => s.toLowerCase()),
            isNot(contains('authorization')),
          );
          expect(request.headers['Referer'], 'https://www.pixiv.net/');
          images++;
          return http.Response.bytes(Uint8List.fromList([1, 2, 3]), 200);
        }),
      );
      addTearDown(api.close);
      await api.connect('refresh-initial-token');
      await Future.wait([
        api.image('https://i.pximg.net/one.png'),
        api.image('https://i.pximg.net/one.png'),
      ]);
      expect(images, 1);
      await api.logout();
      expect(store.value, isNull);
      expect(
        () => api.image('https://i.pximg.net/one.png'),
        throwsA(isA<ApiFailure>()),
      );
    },
  );

  test(
    'parallel requests share token renewal and refresh identity is checked',
    () async {
      final store = TokenStore()
        ..value = {'refresh_token': 'refresh-initial-token', 'user_id': '42'};
      var authCalls = 0;
      final gate = Completer<void>();
      final api = PixivDirectApi(
        store: store,
        scope: 'test',
        client: MockClient((request) async {
          if (request.url.host == 'oauth.secure.pixiv.net') {
            authCalls++;
            await gate.future;
            return login();
          }
          return jsonResponse({'illusts': [], 'next_url': null});
        }),
      );
      addTearDown(api.close);
      await api.restore();
      final a = api.page(mode: 'bookmarks', category: 'illustration');
      final b = api.page(mode: 'bookmarks', category: 'illustration');
      gate.complete();
      await Future.wait([a, b]);
      expect(authCalls, 1);
    },
  );

  test(
    'logout while OAuth is pending prevents late credential storage',
    () async {
      final store = TokenStore();
      final gate = Completer<http.Response>();
      final api = PixivDirectApi(
        store: store,
        scope: 'test',
        client: MockClient((_) => gate.future),
      );
      addTearDown(api.close);
      final loginFuture = api.connect('refresh-initial-token');
      final failure = expectLater(loginFuture, throwsA(isA<ApiFailure>()));
      await api.logout();
      gate.complete(login());
      await failure;
      expect(store.value, isNull);
      expect(store.writes, 0);
    },
  );

  test(
    'novel JSON extraction handles quoted braces without executing HTML',
    () async {
      final text = 'First paragraph\n\nQuoted " } isOwnWork and { nested text';
      final api = PixivDirectApi(
        store: TokenStore(),
        scope: 'test',
        client: MockClient((request) async {
          if (request.url.host == 'oauth.secure.pixiv.net') return login();
          expect(request.url.path, '/webview/v2/novel');
          return http.Response(
            'not JavaScript execution; novel: ${jsonEncode({'id': 42, 'text': text})}, isOwnWork: false',
            200,
          );
        }),
      );
      addTearDown(api.close);
      await api.connect('refresh-initial-token');
      expect(await api.novelText('42'), text);
    },
  );

  test('novel metadata uses the official v2 detail endpoint', () async {
    final api = PixivDirectApi(
      store: TokenStore(),
      scope: 'test',
      client: MockClient((request) async {
        if (request.url.host == 'oauth.secure.pixiv.net') return login();
        expect(request.url.path, '/v2/novel/detail');
        expect(request.url.queryParameters['novel_id'], '42');
        return jsonResponse({
          'novel': {...work(42), '_novel': true},
        });
      }),
    );
    addTearDown(api.close);
    await api.connect('refresh-initial-token');
    expect((await api.detail('42', novel: true))['id'], 42);
  });

  test('a late login cannot replace a newer imported identity', () async {
    final store = TokenStore();
    final pending = Completer<http.Response>();
    var calls = 0;
    final api = PixivDirectApi(
      store: store,
      scope: 'test',
      client: MockClient((_) async {
        return ++calls == 1 ? pending.future : login('99');
      }),
    );
    addTearDown(api.close);
    final old = api.connect('refresh-old-token');
    final rejected = expectLater(old, throwsA(isA<ApiFailure>()));
    await api.connect('refresh-new-token');
    pending.complete(login('42'));
    await rejected;
    expect(api.userId, '99');
    expect(store.value?['user_id'], '99');
    expect(store.writes, 1);
  });

  test('image directories must match all pages and stay on official CDN', () {
    expect(PixivDirectApi.pages(work(42)), ['https://i.pximg.net/42.png']);
    expect(
      () => PixivDirectApi.pages({...work(42), 'page_count': 2}),
      throwsA(isA<ApiFailure>()),
    );
    expect(
      () => PixivDirectApi.imageUri('https://i.pximg.net.evil.test/a.jpg'),
      throwsA(isA<ApiFailure>()),
    );
    expect(
      () => PixivDirectApi.imageUri('https://secret@i.pximg.net/a.jpg'),
      throwsA(isA<ApiFailure>()),
    );
  });

  testWidgets(
    'native entry imports only after administrator verification and reads directly',
    (tester) async {
      tester.view.physicalSize = const Size(390, 1200);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final serverCalls = <http.Request>[];
      final library = LibraryApi(
        server: Uri.parse('https://library.test'),
        token: 'device-token',
        client: MockClient((request) async {
          serverCalls.add(request);
          expect(request.url.path, '/api/pixiv/direct/credentials');
          expect(jsonDecode(request.body)['admin_password'], 'admin-synthetic');
          return jsonResponse({'refresh_token': 'refresh-initial-token'});
        }),
      );
      addTearDown(library.close);
      var pixivCalls = 0;
      final direct = PixivDirectApi(
        store: TokenStore(),
        scope: 'test',
        client: MockClient((request) async {
          pixivCalls++;
          if (request.url.host == 'oauth.secure.pixiv.net') return login();
          return jsonResponse({
            'illusts': [work(42)],
            'next_url': null,
          });
        }),
      );
      await tester.pumpWidget(
        MaterialApp(
          home: PixivDirectScreen(library: library, direct: direct),
        ),
      );
      await tester.pumpAndSettle();
      expect(serverCalls, isEmpty);
      await tester.tap(find.widgetWithText(FilledButton, '导入主账号令牌'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField).last, 'admin-synthetic');
      await tester.tap(find.widgetWithText(FilledButton, '导入'));
      for (
        var i = 0;
        i < 30 && find.text('Synthetic 42').evaluate().isEmpty;
        i++
      ) {
        await tester.runAsync(() async {
          await Future<void>.delayed(const Duration(milliseconds: 5));
        });
        await tester.pump(const Duration(milliseconds: 10));
      }
      expect(find.text('Synthetic 42'), findsOneWidget);
      expect(find.text('Synthetic account · 手机直连'), findsOneWidget);
      expect(serverCalls.length, 1);
      expect(pixivCalls, 2);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const MaterialApp(home: SizedBox()));
    },
  );
}
