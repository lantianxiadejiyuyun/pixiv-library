import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/watchlist_screen.dart';

Map<String, dynamic> row(int id) => {
  'id': 's$id',
  'kind': 'novel',
  'title': '合成系列 $id',
  'author': '作者',
  'content_count': 3,
  'has_update': true,
  'auto_download': false,
  'last_error': null,
};
Future<void> settle(WidgetTester tester) async {
  await tester.pump();
  await tester.runAsync(
    () => Future<void>.delayed(const Duration(milliseconds: 60)),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('原生追更类型搜索与未读筛选传给服务器', (tester) async {
    final calls = <http.Request>[];
    final api = LibraryApi(
      server: Uri.parse('http://192.168.1.20:8800'),
      token: 'synthetic',
      client: MockClient((request) async {
        calls.add(request);
        return http.Response(
          jsonEncode({
            'items': [row(1)],
            'total': 1,
            'sync': {'pending_tasks': 0},
          }),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );
    addTearDown(api.close);
    await tester.pumpWidget(
      MaterialApp(
        home: WatchlistScreen(api: api, onOpenItem: (_) {}),
      ),
    );
    await settle(tester);
    expect(find.text('合成系列 1'), findsOneWidget);
    await tester.tap(find.text('漫画'));
    await settle(tester);
    expect(calls.last.url.queryParameters['kind'], 'manga');
    await tester.tap(find.text('未读更新'));
    await settle(tester);
    expect(calls.last.url.queryParameters['updates'], 'true');
    await tester.enterText(find.byType(TextField), '检索');
    await tester.pump(const Duration(milliseconds: 400));
    await settle(tester);
    expect(calls.last.url.queryParameters['q'], '检索');
  });
  testWidgets('返回系列列表不重新请求；标记已读和检查是独立操作', (tester) async {
    final calls = <http.Request>[];
    final api = LibraryApi(
      server: Uri.parse('http://192.168.1.20:8800'),
      token: 'synthetic',
      client: MockClient((request) async {
        calls.add(request);
        if (request.method == 'POST') return http.Response('{}', 200);
        if (request.url.path.endsWith('/s1')) {
          return http.Response(
            jsonEncode({
              'catalog': {
                'entries': [
                  {'item_id': 'b1', 'title': '合成章节', 'content_state': 'ready'},
                ],
              },
            }),
            200,
            headers: {'content-type': 'application/json; charset=utf-8'},
          );
        }
        return http.Response(
          jsonEncode({
            'items': [row(1)],
            'total': 1,
            'sync': {'pending_tasks': 0},
          }),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );
    addTearDown(api.close);
    final opened = <String>[];
    await tester.pumpWidget(
      MaterialApp(
        home: WatchlistScreen(api: api, onOpenItem: opened.add),
      ),
    );
    await settle(tester);
    await tester.tap(find.text('合成系列 1'));
    await settle(tester);
    await tester.tap(find.text('标为已读'));
    await settle(tester);
    expect(calls.last.url.path, '/api/pixiv/watchlist/s1/read');
    await tester.tap(find.text('检查目录'));
    await settle(tester);
    expect(calls.last.url.path, '/api/pixiv/watchlist/s1/check');
    await tester.tap(find.text('1. 合成章节'));
    expect(opened, ['b1']);
    await tester.pageBack();
    await settle(tester);
    expect(calls.where((r) => r.url.path == '/api/pixiv/watchlist').length, 1);
    expect(find.text('更新'), findsNothing);
  });
  testWidgets('弱网刷新保留追更列表和重试入口', (tester) async {
    var fail = false;
    final api = LibraryApi(
      server: Uri.parse('http://192.168.1.20:8800'),
      token: 'synthetic',
      client: MockClient((request) async {
        if (fail) {
          return http.Response(
            '{"detail":"网络暂不可用"}',
            503,
            headers: {'content-type': 'application/json; charset=utf-8'},
          );
        }
        return http.Response(
          jsonEncode({
            'items': [row(1)],
            'total': 1,
            'sync': {'pending_tasks': 0},
          }),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );
    addTearDown(api.close);
    await tester.pumpWidget(
      MaterialApp(
        home: WatchlistScreen(api: api, onOpenItem: (_) {}),
      ),
    );
    await settle(tester);
    fail = true;
    await tester.enterText(find.byType(TextField), '新检索');
    await tester.pump(const Duration(milliseconds: 400));
    await settle(tester);
    expect(find.text('合成系列 1'), findsOneWidget);
    expect(find.text('重试'), findsOneWidget);
  });
}
