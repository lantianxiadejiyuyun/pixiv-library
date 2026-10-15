// Explicit opt-in fixture test. Never runs as part of `flutter test` by default.
import 'dart:convert';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/session.dart';
import '../test/session_api_test.dart' show MemoryStore;

void main() {
  test('binds to disposable server, reads manga/novel/series and persists progress, then disconnects', () async {
    const path = String.fromEnvironment('CONTRACT_FIXTURE');
    if (path.isEmpty) throw StateError('Provide a disposable fixture file via CONTRACT_FIXTURE');
    final fixture = jsonDecode(await File(path).readAsString()) as Map<String, dynamic>;
    final code = PairingCode.parse(fixture['qr_payload'] as String);
    // Only our dedicated loopback fixture is permitted, never the production port.
    expect(code.server.host, '127.0.0.1');
    expect(code.server.port, 8801);
    final store = MemoryStore();
    final session = await PairingService(store).bind(code, 'Flutter 契约验证设备');
    final api = LibraryApi.forSession(session);
    final anonymous = LibraryApi(server: code.server);
    try {
      final items = await api.get('/items', query: {'source': 'local'});
      expect((items['items'] as List).length, 3);
      final novel = await api.get('/items/${fixture['upper']}');
      final assetId = novel['assets'][0]['id'];
      final chapters = await api.get('/assets/$assetId/text-sections');
      expect((chapters['sections'] as List).length, 3);
      await api.put('/items/${fixture['upper']}/progress', {'page': 0, 'section': 1, 'offset': .375});
      final updated = await api.get('/items/${fixture['upper']}');
      expect(updated['progress']['section'], 1);
      expect(updated['progress']['offset'], .375);
      final manga = await api.get('/items/${fixture['manga']}');
      expect((manga['assets'] as List).length, 3);
      final image = await api.image('/assets/${manga['assets'][2]['id']}/content');
      expect(image.length, greaterThan(100));
      expect(image.take(4).toList(), [137, 80, 78, 71]);
      await api.put('/items/${fixture['manga']}/progress', {'page': 2});
      final series = await api.get('/series/${fixture['series']}');
      expect((series['entries'] as List).length, 3);
      await expectLater(api.get('/settings'), throwsA(isA<ApiFailure>().having((e) => e.status, 'status', 403)));
      await expectLater(anonymous.post('/devices/pairing/exchange', {'pairing_token': code.token, 'device_name': 'replay'}),
        throwsA(isA<ApiFailure>().having((e) => e.status, 'status', 400)));
      await api.post('/items/${fixture['missing']}/download', {});
      await api.post('/devices/disconnect');
      await expectLater(api.get('/items'), throwsA(isA<ApiFailure>().having((e) => e.status, 'status', 401)));
    } finally {
      try { await api.post('/devices/disconnect'); } catch (_) {}
      api.close(); anonymous.close();
    }
  });
}
