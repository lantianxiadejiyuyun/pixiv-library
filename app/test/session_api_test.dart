import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/session.dart';

final pairingToken = 'ql_pair_${'a' * 43}';
final deviceToken = 'ql_device_${'b' * 64}';
String qr([String server = 'http://192.168.1.12:8800']) => jsonEncode({
  'type': 'qiye-library',
  'version': 1,
  'server_url': server,
  'pairing_token': pairingToken,
});
Map<String, dynamic> grant() => {
  'device_id': 'device-1',
  'device_name': '手机',
  'access_token': deviceToken,
  'expires_at': DateTime.now()
      .add(const Duration(days: 90))
      .toUtc()
      .toIso8601String(),
};

class MemoryStore implements SessionStore {
  DeviceSession? value;
  bool failWrite = false;
  @override
  Future<void> clear() async {
    value = null;
  }

  @override
  Future<DeviceSession?> read() async => value;
  @override
  Future<void> write(DeviceSession session) async {
    if (failWrite) throw StateError('storage unavailable');
    value = session;
  }
}

void main() {
  group('Binding code validation', () {
    test('accepts private IPv4, IPv6, LAN names and public HTTPS', () {
      for (final url in [
        'http://192.168.1.12:8800',
        'http://10.0.2.2:8800',
        'http://172.16.0.2',
        'http://nas.local:8800',
        'http://nas:8800',
        'http://[fd00::12]:8800',
        'https://library.example.com',
      ]) {
        expect(PairingCode.parse(qr(url)).server.scheme, startsWith('http'));
      }
    });
    test(
      'rejects public HTTP, non-root URLs, credentials and malformed IP aliases',
      () {
        for (final url in [
          'http://example.com',
          'http://8.8.8.8',
          'https://example.com/library',
          'https://user:password@example.com',
          'https://example.com/?secret=1',
          'https://example.com/#x',
          'http://2130706433',
          'http://127.1',
          'http://0x7f000001',
          'file:///tmp/a',
          'http://0.0.0.0',
          'http://224.0.0.1',
          'https://example.com:0',
          'http://nas\\evil',
        ]) {
          expect(
            () => PairingCode.parse(qr(url)),
            throwsFormatException,
            reason: url,
          );
        }
      },
    );
    test('rejects foreign QR, version, short token and excessive payload', () {
      for (final raw in [
        'https://example.com',
        '{}',
        qr().replaceFirst('"version":1', '"version":2'),
        qr().replaceFirst(pairingToken, 'secret'),
        'x' * 4097,
      ]) {
        expect(() => PairingCode.parse(raw), throwsFormatException);
      }
    });
  });
  group('Authenticated transport', () {
    test('sends token in header only and refuses redirects', () async {
      final api = LibraryApi(
        server: Uri.parse('http://192.168.1.12:8800'),
        token: deviceToken,
        client: MockClient((request) async {
          expect(request.headers['Authorization'], 'Bearer $deviceToken');
          expect(
            request.url.toString(),
            'http://192.168.1.12:8800/api/items?page=1',
          );
          expect(request.followRedirects, isFalse);
          return http.Response(
            '',
            302,
            headers: {'location': 'https://outside.example/'},
          );
        }),
      );
      await expectLater(
        api.get('/items', query: {'page': '1'}),
        throwsA(isA<ApiFailure>()),
      );
      api.close();
    });
    test('never contacts foreign origin or non API resources', () async {
      var requests = 0;
      final api = LibraryApi(
        server: Uri.parse('https://nas.example'),
        token: deviceToken,
        client: MockClient((r) async {
          requests++;
          return http.Response('{}', 200);
        }),
      );
      for (final url in [
        'https://evil.example/api/items',
        '//evil.example/api/items',
        'https://nas.example/private',
        'https://nas.example/api/items?access_token=x',
        '/api/items#x',
      ]) {
        await expectLater(api.get(url), throwsA(isA<ApiFailure>()));
      }
      expect(requests, 0);
      api.close();
    });
    test(
      '401 invalidates once and remote errors never leak raw body',
      () async {
        var invalidations = 0;
        final api = LibraryApi(
          server: Uri.parse('https://nas.example'),
          token: deviceToken,
          onUnauthorized: () {
            invalidations++;
          },
          client: MockClient(
            (r) async => http.Response('secret $deviceToken', 401),
          ),
        );
        for (var i = 0; i < 2; i++) {
          try {
            await api.get('/items');
            fail('expected unauthorized');
          } on ApiFailure catch (e) {
            expect(e.status, 401);
            expect(e.message.contains(deviceToken), isFalse);
          }
        }
        expect(invalidations, 1);
        api.close();
      },
    );
    test('oversized response is rejected before decoding', () async {
      final api = LibraryApi(
        server: Uri.parse('https://nas.example'),
        client: MockClient.streaming(
          (request, body) async => http.StreamedResponse(
            const Stream.empty(),
            200,
            contentLength: 70 * 1024 * 1024,
          ),
        ),
      );
      await expectLater(
        api.image('/items/1/cover'),
        throwsA(isA<ApiFailure>()),
      );
      api.close();
    });
  });
  group('Pairing exchange', () {
    test('duplicate camera frames cannot consume code twice', () async {
      final pending = Completer<http.Response>();
      var calls = 0;
      final store = MemoryStore();
      final service = PairingService(
        store,
        createApi: (uri) => LibraryApi(
          server: uri,
          client: MockClient((request) {
            calls++;
            expect(request.headers.containsKey('Authorization'), isFalse);
            expect(jsonDecode(request.body)['pairing_token'], pairingToken);
            return pending.future;
          }),
        ),
      );
      final first = service.bind(PairingCode.parse(qr()), '手机');
      await expectLater(
        service.bind(PairingCode.parse(qr()), '手机'),
        throwsA(isA<ApiFailure>()),
      );
      pending.complete(http.Response.bytes(utf8.encode(jsonEncode(grant())), 200));
      final session = await first;
      expect(calls, 1);
      expect(store.value, same(session));
      expect(session.token, deviceToken);
    });
    test(
      'storage failure revokes newly issued token and does not report success',
      () async {
        final store = MemoryStore()..failWrite = true;
        var revoked = false;
        final service = PairingService(
          store,
          createApi: (uri) => LibraryApi(
            server: uri,
            client: MockClient(
              (r) async => http.Response.bytes(utf8.encode(jsonEncode(grant())), 200),
            ),
          ),
          createAuthorizedApi: (session) => LibraryApi(
            server: session.server,
            token: session.token,
            client: MockClient((r) async {
              expect(r.url.path, '/api/devices/disconnect');
              expect(r.headers['Authorization'], 'Bearer $deviceToken');
              revoked = true;
              return http.Response('{}', 200);
            }),
          ),
        );
        await expectLater(
          service.bind(PairingCode.parse(qr()), '手机'),
          throwsA(isA<ApiFailure>()),
        );
        expect(revoked, isTrue);
        expect(store.value, isNull);
      },
    );
    test(
      'expired QR yields actionable retry without storing a credential',
      () async {
        final store = MemoryStore();
        final service = PairingService(
          store,
          createApi: (uri) => LibraryApi(
            server: uri,
            client: MockClient(
              (r) async =>
                  http.Response('{"detail":"sensitive diagnostics"}', 400),
            ),
          ),
        );
        try {
          await service.bind(PairingCode.parse(qr()), '手机');
          fail('expected failure');
        } on ApiFailure catch (e) {
          expect(e.message, contains('重新生成'));
          expect(e.message, isNot(contains('sensitive')));
        }
        expect(store.value, isNull);
      },
    );
  });
}
