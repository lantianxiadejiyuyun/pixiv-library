import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:qiye_library/api.dart';
import 'package:qiye_library/server_speed_screen.dart';
import 'package:qiye_library/server_speed_test.dart';

class FixedWatch extends Stopwatch {
  final Duration duration;
  FixedWatch(this.duration);
  @override
  Duration get elapsed => duration;
}

class RealHttpOverrides extends HttpOverrides {}

class ScriptClient extends http.BaseClient {
  final Future<http.StreamedResponse> Function(http.BaseRequest) handler;
  bool closed = false;
  int requests = 0;
  ScriptClient(this.handler);
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) {
    requests++;
    return handler(request);
  }

  @override
  void close() => closed = true;
}

http.StreamedResponse jsonResponse(Object value, [int status = 200]) {
  final bytes = utf8.encode(jsonEncode(value));
  return http.StreamedResponse(
    Stream.value(bytes),
    status,
    contentLength: bytes.length,
    headers: {'content-type': 'application/json'},
  );
}

Future<http.StreamedResponse> success(http.BaseRequest request) async {
  if (request.url.path.endsWith('/ping')) return jsonResponse({'ok': true});
  if (request.url.path.endsWith('/download')) {
    final count = int.parse(request.url.queryParameters['bytes']!);
    final block = Uint8List(1024);
    Stream<List<int>> chunks() async* {
      var left = count;
      while (left > 0) {
        final size = left > block.length ? block.length : left;
        yield Uint8List.sublistView(block, 0, size);
        left -= size;
      }
    }

    return http.StreamedResponse(
      chunks(),
      200,
      contentLength: count,
      headers: {'content-type': 'application/octet-stream'},
    );
  }
  if (request.url.path.endsWith('/upload')) {
    final count = await request.finalize().fold<int>(0, (a, b) => a + b.length);
    expect(count, request.contentLength);
    return jsonResponse({'received_bytes': count, 'elapsed_ms': 0.01});
  }
  return jsonResponse({'ok': true});
}

LibraryApi library({http.Client? client, void Function()? unauthorized}) =>
    LibraryApi(
      server: Uri.parse('https://library.test'),
      token: 'secret-device',
      client: client,
      onUnauthorized: unauthorized,
    );

void main() {
  test(
    'median latency and acknowledged transfer rates use actual bytes',
    () async {
      final shared = ScriptClient(success);
      final api = library(client: shared);
      final requests = <http.BaseRequest>[];
      final transport = ScriptClient((request) async {
        requests.add(request);
        expect(request.url.origin, api.server.origin);
        expect(request.url.toString(), isNot(contains('secret-device')));
        expect(request.followRedirects, isFalse);
        expect(request.headers['Authorization'], 'Bearer secret-device');
        expect(request.headers['Accept-Encoding'], 'identity');
        expect(request.headers['Cache-Control'], contains('no-store'));
        return success(request);
      });
      final durations = [30, 10, 20, 90, 40, 2000, 4000];
      var watch = 0;
      final test = ServerSpeedTest(
        api: api,
        clientFactory: () => transport,
        stopwatchFactory: () =>
            FixedWatch(Duration(milliseconds: durations[watch++])),
      );
      addTearDown(api.close);
      addTearDown(test.dispose);
      await test.start(transferBytes: 1048576);
      expect(test.snapshot.stage, SpeedTestStage.complete);
      expect(test.snapshot.latencyMs, 30);
      expect(test.snapshot.download!.bytes, 1048576);
      expect(
        test.snapshot.download!.megabytesPerSecond,
        closeTo(0.524288, 0.000001),
      );
      expect(
        test.snapshot.download!.megabitsPerSecond,
        closeTo(4.194304, 0.000001),
      );
      expect(
        test.snapshot.upload!.megabitsPerSecond,
        closeTo(2.097152, 0.000001),
      );
      expect(requests.length, 7);
      expect(
        requests
            .take(6)
            .map((r) => r.url.queryParameters['nonce'])
            .toSet()
            .length,
        6,
      );
      expect(transport.closed, isTrue);
      expect(shared.closed, isFalse);
      expect(await api.get('/dashboard'), {'ok': true});
    },
  );

  for (final status in [301, 401, 403, 404, 429, 500]) {
    test(
      'HTTP $status is bounded and does not expose response diagnostics',
      () async {
        var expired = 0;
        final api = library(unauthorized: () => expired++);
        final transport = ScriptClient(
          (_) async => jsonResponse({'error': 'secret-diagnostic'}, status),
        );
        final test = ServerSpeedTest(api: api, clientFactory: () => transport);
        addTearDown(api.close);
        addTearDown(test.dispose);
        await test.start(transferBytes: 32);
        expect(test.snapshot.stage, SpeedTestStage.failed);
        expect(test.snapshot.error, isNot(contains('secret-diagnostic')));
        expect(test.snapshot.error, isNot(contains('secret-device')));
        expect(expired, status == 401 ? 1 : 0);
        expect(transport.requests, 1);
        expect(transport.closed, isTrue);
        if (status == 404) expect(test.snapshot.error, contains('更新后端'));
      },
    );
  }

  for (final kind in ['short', 'excess', 'compressed', 'wrong-type']) {
    test('download rejects $kind payload without inventing a speed', () async {
      final api = library();
      final transport = ScriptClient((request) async {
        if (!request.url.path.endsWith('/download')) return success(request);
        return http.StreamedResponse(
          Stream.value(
            Uint8List(
              kind == 'short'
                  ? 31
                  : kind == 'excess'
                  ? 33
                  : 32,
            ),
          ),
          200,
          contentLength: 32,
          headers: {
            'content-type': kind == 'wrong-type'
                ? 'text/html'
                : 'application/octet-stream',
            if (kind == 'compressed') 'content-encoding': 'gzip',
          },
        );
      });
      final test = ServerSpeedTest(
        api: api,
        clientFactory: () => transport,
        stopwatchFactory: () => FixedWatch(const Duration(milliseconds: 1)),
      );
      addTearDown(api.close);
      addTearDown(test.dispose);
      await test.start(transferBytes: 32);
      expect(test.snapshot.stage, SpeedTestStage.failed);
      expect(test.snapshot.latencyMs, 1);
      expect(test.snapshot.download, isNull);
      expect(test.snapshot.upload, isNull);
      expect(transport.requests, 6);
    });
  }

  test(
    'upload must receive complete acknowledgement and preserves download on failure',
    () async {
      final api = library();
      final transport = ScriptClient(
        (request) async => request.url.path.endsWith('/upload')
            ? jsonResponse({'received_bytes': 31})
            : success(request),
      );
      final test = ServerSpeedTest(
        api: api,
        clientFactory: () => transport,
        stopwatchFactory: () => FixedWatch(const Duration(seconds: 1)),
      );
      addTearDown(api.close);
      addTearDown(test.dispose);
      await test.start(transferBytes: 32);
      expect(test.snapshot.stage, SpeedTestStage.failed);
      expect(test.snapshot.download!.bytes, 32);
      expect(test.snapshot.upload, isNull);
      expect(test.snapshot.error, contains('未确认'));
    },
  );

  test(
    'duplicate start shares a run; cancel then retry ignores a late response',
    () async {
      final api = library();
      final late = Completer<http.StreamedResponse>();
      final old = ScriptClient((_) => late.future);
      final next = ScriptClient(success);
      var clients = 0;
      final test = ServerSpeedTest(
        api: api,
        clientFactory: () => clients++ == 0 ? old : next,
        stopwatchFactory: () => FixedWatch(const Duration(seconds: 1)),
      );
      addTearDown(api.close);
      addTearDown(test.dispose);
      final first = test.start(transferBytes: 32);
      expect(identical(first, test.start()), isTrue);
      test.cancel();
      expect(old.closed, isTrue);
      final retry = test.start(transferBytes: 64);
      await retry;
      await first;
      expect(test.snapshot.stage, SpeedTestStage.complete);
      expect(test.snapshot.download!.bytes, 64);
      var streamCancelled = false;
      final lateStream = StreamController<List<int>>(
        onCancel: () {
          streamCancelled = true;
        },
      );
      late.complete(http.StreamedResponse(lateStream.stream, 401));
      await Future<void>.delayed(Duration.zero);
      expect(streamCancelled, isTrue);
      expect(test.snapshot.stage, SpeedTestStage.complete);
      expect(clients, 2);
      await lateStream.close();
    },
  );

  test(
    'cancel during upload waits for no acknowledgement and retains download',
    () async {
      final api = library();
      final ack = Completer<http.StreamedResponse>();
      final buffered = Completer<void>();
      final transport = ScriptClient((request) async {
        if (!request.url.path.endsWith('/upload')) return success(request);
        await request.finalize().drain<void>();
        buffered.complete();
        return ack.future;
      });
      final test = ServerSpeedTest(
        api: api,
        clientFactory: () => transport,
        stopwatchFactory: () => FixedWatch(const Duration(seconds: 1)),
      );
      addTearDown(api.close);
      addTearDown(test.dispose);
      final running = test.start(transferBytes: 32);
      await buffered.future;
      expect(test.snapshot.stage, SpeedTestStage.upload);
      expect(test.snapshot.upload, isNull);
      test.cancel();
      await running;
      expect(test.snapshot.stage, SpeedTestStage.cancelled);
      expect(test.snapshot.download!.bytes, 32);
      expect(transport.closed, isTrue);
      ack.complete(jsonResponse({'received_bytes': 32}));
      await Future<void>.delayed(Duration.zero);
      expect(test.snapshot.upload, isNull);
    },
  );

  test(
    'whole-stage timeout closes the client, even if send does not resolve',
    () async {
      final api = library();
      final transport = ScriptClient(
        (_) => Completer<http.StreamedResponse>().future,
      );
      final test = ServerSpeedTest(
        api: api,
        clientFactory: () => transport,
        stageTimeout: const Duration(milliseconds: 15),
        idleTimeout: const Duration(milliseconds: 15),
      );
      addTearDown(api.close);
      addTearDown(test.dispose);
      await test.start(transferBytes: 32);
      expect(test.snapshot.stage, SpeedTestStage.failed);
      expect(test.snapshot.error, contains('超时'));
      expect(transport.closed, isTrue);
    },
  );

  test('expired pairing fails before transport sends credentials', () async {
    var expired = 0;
    final api = LibraryApi(
      server: Uri.parse('https://library.test'),
      token: 'old-token',
      expiresAt: DateTime(2000),
      onUnauthorized: () => expired++,
    );
    final transport = ScriptClient(success);
    final test = ServerSpeedTest(api: api, clientFactory: () => transport);
    addTearDown(api.close);
    addTearDown(test.dispose);
    await test.start(transferBytes: 32);
    expect(expired, 1);
    expect(transport.requests, 0);
    expect(test.snapshot.error, contains('授权已失效'));
  });

  test('real HTTP stream uploads and downloads exact bytes', () async {
    await HttpOverrides.runWithHttpOverrides(() async {
      final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      final received = <int>[];
      final handlers = <Future<void>>[];
      server.listen((request) {
        handlers.add(() async {
          expect(
            request.headers.value('authorization'),
            'Bearer socket-device',
          );
          if (request.uri.path.endsWith('/download')) {
            final count = int.parse(request.uri.queryParameters['bytes']!);
            request.response.headers.contentType = ContentType.binary;
            request.response.contentLength = count;
            final block = Uint8List(4096);
            for (var i = 0; i < count; i += block.length) {
              request.response.add(block);
            }
          } else if (request.uri.path.endsWith('/upload')) {
            final count = await request.fold<int>(0, (a, b) => a + b.length);
            received.add(count);
            request.response.write(jsonEncode({'received_bytes': count}));
          } else {
            request.response.write('{"ok":true}');
          }
          await request.response.close();
        }());
      });
      final api = LibraryApi(
        server: Uri.parse('http://127.0.0.1:${server.port}'),
        token: 'socket-device',
      );
      final speed = ServerSpeedTest(api: api);
      try {
        await speed.start(transferBytes: 1024 * 1024);
        expect(speed.snapshot.error, isNull);
        expect(speed.snapshot.stage, SpeedTestStage.complete);
        expect(speed.snapshot.download!.bytes, 1024 * 1024);
        expect(speed.snapshot.upload!.bytes, 1024 * 1024);
        expect(received, [1024 * 1024]);
        expect(speed.snapshot.upload!.elapsed.inMicroseconds, greaterThan(0));
      } finally {
        speed.dispose();
        api.close();
        await server.close(force: true);
        await Future.wait(handlers);
      }
    }, RealHttpOverrides());
  });

  testWidgets(
    'native speed screen shows results on narrow screen with large text',
    (tester) async {
      tester.view.physicalSize = const Size(320, 1200);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final api = library();
      addTearDown(api.close);
      final speed = ServerSpeedTest(
        api: api,
        clientFactory: () => ScriptClient(success),
        stopwatchFactory: () => FixedWatch(const Duration(milliseconds: 1)),
      );
      await tester.pumpWidget(
        MaterialApp(
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(
              context,
            ).copyWith(textScaler: const TextScaler.linear(1.8)),
            child: child!,
          ),
          home: ServerSpeedScreen(api: api, tester: speed),
        ),
      );
      expect(find.text('服务器测速'), findsOneWidget);
      expect(find.text('https://library.test'), findsOneWidget);
      await tester.runAsync(() => speed.start());
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
        find.text('测速完成'),
        200,
        scrollable: find.byType(Scrollable).first,
      );
      expect(find.textContaining('67108.86 Mbps'), findsNWidgets(2));
      expect(find.textContaining('8388.61 MB/s'), findsNWidgets(2));
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const MaterialApp(home: SizedBox()));
    },
  );

  testWidgets('leaving or backgrounding the screen cancels its transfer', (
    tester,
  ) async {
    final api = library();
    addTearDown(api.close);
    final transport = ScriptClient(
      (_) => Completer<http.StreamedResponse>().future,
    );
    final speed = ServerSpeedTest(api: api, clientFactory: () => transport);
    await tester.pumpWidget(
      MaterialApp(
        home: ServerSpeedScreen(api: api, tester: speed),
      ),
    );
    final running = speed.start();
    await tester.pump();
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    await tester.pump();
    await running;
    expect(speed.snapshot.stage, SpeedTestStage.cancelled);
    expect(transport.closed, isTrue);
    await tester.pumpWidget(const MaterialApp(home: SizedBox()));
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    expect(tester.takeException(), isNull);
  });

  testWidgets('leaving cancels a stalled download body and its idle deadline', (
    tester,
  ) async {
    final api = library();
    addTearDown(api.close);
    var streamCancelled = false;
    final stream = StreamController<List<int>>(
      onCancel: () {
        streamCancelled = true;
      },
    );
    late Completer<void> started;
    final transport = ScriptClient((request) async {
      if (!request.url.path.endsWith('/download')) return success(request);
      started.complete();
      return http.StreamedResponse(
        stream.stream,
        200,
        contentLength: 8 * 1024 * 1024,
        headers: {'content-type': 'application/octet-stream'},
      );
    });
    final speed = ServerSpeedTest(
      api: api,
      clientFactory: () => transport,
      stopwatchFactory: () => FixedWatch(const Duration(milliseconds: 1)),
    );
    await tester.pumpWidget(
      MaterialApp(
        home: ServerSpeedScreen(api: api, tester: speed),
      ),
    );
    late Future<void> running;
    await tester.runAsync(() async {
      started = Completer<void>();
      running = speed.start();
      await started.future.timeout(const Duration(seconds: 3));
      await Future<void>.delayed(Duration.zero);
    });
    expect(started.isCompleted, isTrue);
    expect(stream.hasListener, isTrue);
    await tester.pumpWidget(const MaterialApp(home: SizedBox()));
    await tester.pump();
    await tester.runAsync(() => running.timeout(const Duration(seconds: 3)));
    await tester.pump();
    expect(streamCancelled, isTrue);
    expect(transport.closed, isTrue);
    expect(tester.takeException(), isNull);
    await tester.runAsync(() async {
      await stream.close();
    });
  });
}
