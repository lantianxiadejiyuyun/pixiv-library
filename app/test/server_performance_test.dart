import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/server_performance_screen.dart';

class PerformanceApi extends LibraryApi {
  int reads = 0;
  bool fail = false;
  PerformanceApi() : super(server: Uri.parse('http://library.test'));
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) async {
    expect(path, '/server/performance');
    reads++;
    if (fail) throw const ApiFailure('测试网络不可用');
    return {
      'status': 'ready',
      'current': {
        'platform': 'Linux',
        'container': true,
        'cpu': {
          'host_percent': 41.2,
          'service_percent': 12.3,
          'logical_cpus': 10,
          'service_capacity': 9,
        },
        'memory': {
          'host': {
            'percent': 50,
            'used_bytes': 4 * 1024 * 1024 * 1024,
            'total_bytes': 8 * 1024 * 1024 * 1024,
          },
          'service': {
            'percent': 20,
            'used_bytes': 1024 * 1024 * 1024,
            'limit_bytes': 5 * 1024 * 1024 * 1024,
          },
        },
        'network': {
          'scope': 'container',
          'download_bps': null,
          'upload_bps': 0,
        },
        'processes': [
          {
            'lane': 'download',
            'pid': 33,
            'state': 'running',
            'cpu_percent': 22,
            'rss_bytes': 1024,
            'cpus': [1, 2],
            'restarts': 0,
          },
        ],
      },
      'history': [
        {'at': 100, 'host_cpu': null, 'service_cpu': null},
        {'at': 102, 'host_cpu': 41.2, 'service_cpu': 12.3},
      ],
      'database': {
        'status': 'ok',
        'stale': false,
        'latency_ms': 3.2,
        'queues': [
          {
            'id': 'download',
            'queued': 99,
            'running': 2,
            'concurrency': 6,
            'effective_paused': true,
          },
        ],
      },
      'storage': [
        {'id': 'library', 'label': '书库存储 / NAS', 'status': 'unavailable'},
      ],
    };
  }

  @override
  Future<dynamic> post(String path, [Map<String, dynamic>? body]) =>
      throw StateError('Performance dashboard must be read only');
}

void main() {
  testWidgets(
    'polling keeps the previous sample on failure and pauses in background',
    (tester) async {
      final api = PerformanceApi();
      addTearDown(api.close);
      await tester.pumpWidget(
        MaterialApp(home: ServerPerformanceScreen(api: api)),
      );
      await tester.pumpAndSettle();
      expect(api.reads, 1);
      expect(find.text('41.2%'), findsOneWidget);
      api.fail = true;
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
      expect(api.reads, 2);
      expect(find.text('41.2%'), findsOneWidget);
      expect(find.text('测试网络不可用'), findsOneWidget);
      await tester.tap(find.byTooltip('暂停刷新'));
      await tester.pump(const Duration(seconds: 5));
      expect(api.reads, 2);
      api.fail = false;
      await tester.tap(find.byTooltip('恢复刷新'));
      await tester.pumpAndSettle();
      expect(api.reads, 3);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      await tester.pump(const Duration(seconds: 5));
      expect(api.reads, 3);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pumpAndSettle();
      expect(api.reads, 4);
      await tester.pumpWidget(const SizedBox());
      await tester.pump(const Duration(seconds: 5));
      expect(api.reads, 4);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'narrow screen and enlarged text show queue and unavailable NAS without overflow',
    (tester) async {
      tester.view.physicalSize = const Size(320, 700);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final api = PerformanceApi();
      addTearDown(api.close);
      await tester.pumpWidget(
        MaterialApp(
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(
              context,
            ).copyWith(textScaler: TextScaler.linear(1.8)),
            child: child!,
          ),
          home: ServerPerformanceScreen(api: api),
        ),
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      await tester.dragUntilVisible(
        find.text('存储暂不可用，其他指标继续采样。'),
        find.byType(ListView),
        const Offset(0, -300),
      );
      await tester.pumpAndSettle();
      expect(find.text('存储暂不可用，其他指标继续采样。'), findsOneWidget);
      await tester.dragUntilVisible(
        find.textContaining('任务 2 / 6'),
        find.byType(ListView),
        const Offset(0, -200),
      );
      await tester.pumpAndSettle();
      expect(find.textContaining('任务 2 / 6'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );
}
