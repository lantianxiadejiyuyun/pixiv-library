import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/library_screen.dart';
import 'package:qiye_library/models.dart';
import 'package:qiye_library/reader_screen.dart';

Map<String, dynamic> book({
  String id = 'novel',
  String state = 'ready',
  bool image = false,
  int count = 1,
  int section = 0,
  double offset = 0,
  bool series = false,
}) => {
  'id': id,
  'title': '作品 $id',
  'type': image ? 'manga' : 'novel',
  'source': 'pixiv',
  'content_state': state,
  'page_count': state == 'ready' ? count : 0,
  'progress': {'page': 0, 'section': section, 'offset': offset},
  'assets': state == 'ready'
      ? [
          for (int i = 0; i < count; i++)
            {
              'id': '$id-$i',
              'position': i,
              'mime_type': image ? 'image/png' : 'text/plain',
            },
        ]
      : [],
  'series': series
      ? [
          {'id': 's1', 'title': '上下篇'},
        ]
      : [],
};

class ReaderApi extends LibraryApi {
  ReaderApi() : super(server: Uri.parse('https://library.test'));
  final saves = <Map<String, dynamic>>[];
  final posts = <String>[];
  final gets = <String>[];
  final queries = <Map<String, String>>[];
  final seriesResponses = <String, Map<String, dynamic>>{};
  bool failProgress = false;
  Completer<void>? holdSave;
  List<Map<String, dynamic>> chapters = [
    {'index': 0, 'title': '短章', 'text': '这是一段短正文。'},
  ];
  Map<String, dynamic> listed = book(state: 'metadata');

  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) async {
    gets.add(path);
    if (query != null) queries.add(query);
    if (seriesResponses.containsKey(path)) return seriesResponses[path];
    if (path.endsWith('/text-sections')) return {'sections': chapters};
    if (path == '/items') {
      return {
        'items': [listed],
        'total': 1,
      };
    }
    if (path == '/items/next') return book(id: 'next', state: 'metadata');
    if (path.startsWith('/items/')) return listed;
    if (path == '/series/s1') {
      return {
        'complete': true,
        'entries': [
          {
            'item_id': 'novel',
            'title': '上篇',
            'position': 0,
            'content_state': 'ready',
            'page_count': 1,
          },
          {
            'item_id': 'next',
            'title': '下篇未下载',
            'position': 1,
            'content_state': 'metadata',
            'page_count': 0,
          },
        ],
      };
    }
    throw const ApiFailure('不存在', 404);
  }

  @override
  Future<dynamic> put(String path, [Map<String, dynamic>? body]) async {
    saves.add({'path': path, ...?body});
    final hold = holdSave;
    holdSave = null;
    if (hold != null) await hold.future;
    if (failProgress) throw const ApiFailure('连接超时');
    return body;
  }

  @override
  Future<dynamic> post(String path, [Map<String, dynamic>? body]) async {
    posts.add(path);
    if (path.endsWith('/visit')) return {'ok': true};
    if (path.endsWith('/download')) {
      listed = {...listed, 'content_state': 'queued'};
    }
    return {'id': 'job'};
  }

  @override
  Future<Uint8List> image(String path) async => base64Decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  );
}

Future<void> phone(WidgetTester tester, Widget child) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
    MaterialApp(theme: ThemeData(useMaterial3: true), home: child),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets(
    'short novel does not become complete on opening; explicit completion saves offset 1',
    (tester) async {
      final api = ReaderApi();
      await phone(
        tester,
        ReaderScreen(api: api, item: LibraryItem.fromJson(book())),
      );
      expect(find.text('0%'), findsOneWidget);
      expect(api.saves, isEmpty);
      await tester.tap(find.text('标记读完'));
      await tester.pumpAndSettle();
      expect(api.saves.single, containsPair('offset', 1.0));
      expect(api.saves.single, containsPair('page', 0));
      expect(find.text('100%'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'restores chapter and scroll position without overwriting saved progress',
    (tester) async {
      final api = ReaderApi()
        ..chapters = [
          {'index': 0, 'title': '第一章', 'text': '前文'},
          {
            'index': 1,
            'title': '第二章',
            'text': List.filled(100, '这是一段供测试阅读位置的正文。').join('\n'),
          },
        ];
      await phone(
        tester,
        ReaderScreen(
          api: api,
          item: LibraryItem.fromJson(book(section: 1, offset: .5)),
        ),
      );
      expect(find.text('第 2 / 2 节'), findsOneWidget);
      expect(find.text('75%'), findsOneWidget);
      expect(api.saves, isEmpty);
      final scroll = tester
          .widget<SingleChildScrollView>(
            find.byType(SingleChildScrollView).first,
          )
          .controller!;
      expect(scroll.offset / scroll.position.maxScrollExtent, closeTo(.5, .01));
      await tester.tap(find.byTooltip('上一节'));
      await tester.pumpAndSettle();
      await tester.pump(const Duration(seconds: 1));
      expect(find.text('第 1 / 2 节'), findsOneWidget);
      expect(api.saves.last, containsPair('section', 0));
      expect(api.saves.last, containsPair('offset', 0.0));
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'failed progress remains visible and prevents accidental exit until retry or discard',
    (tester) async {
      final api = ReaderApi()..failProgress = true;
      await phone(
        tester,
        Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () => Navigator.push(
                context,
                MaterialPageRoute<void>(
                  builder: (_) => ReaderScreen(
                    api: api,
                    item: LibraryItem.fromJson(book()),
                  ),
                ),
              ),
              child: const Text('进入阅读'),
            ),
          ),
        ),
      );
      await tester.tap(find.text('进入阅读'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('标记读完'));
      await tester.pumpAndSettle();
      expect(find.textContaining('阅读进度未保存'), findsOneWidget);
      await tester.tap(find.byTooltip('返回书库'));
      await tester.pumpAndSettle();
      expect(find.text('阅读进度尚未保存'), findsOneWidget);
      expect(find.byType(ReaderScreen), findsOneWidget);
      await tester.tap(find.text('返回并重试'));
      await tester.pumpAndSettle();
      api.failProgress = false;
      await tester.tap(find.text('重试'));
      await tester.pumpAndSettle();
      expect(find.textContaining('阅读进度未保存'), findsNothing);
      await tester.tap(find.byTooltip('返回书库'));
      await tester.pumpAndSettle();
      expect(find.byType(ReaderScreen), findsNothing);
    },
  );

  testWidgets(
    'image paging stays responsive while old progress save is pending and saves latest zero-based page',
    (tester) async {
      final hold = Completer<void>();
      final api = ReaderApi()..holdSave = hold;
      await phone(
        tester,
        ReaderScreen(
          api: api,
          item: LibraryItem.fromJson(book(id: 'comic', image: true, count: 3)),
        ),
      );
      await tester.pump(const Duration(seconds: 1));
      await tester.tap(find.byTooltip('下一 P'));
      await tester.pump();
      await tester.tap(find.byTooltip('下一 P'));
      await tester.pump();
      expect(find.text('第 3 / 3 P'), findsOneWidget);
      await tester.pump(const Duration(seconds: 1));
      hold.complete();
      await tester.pumpAndSettle();
      expect(api.saves.last, containsPair('page', 2));
      expect(
        api.saves.every((s) => s['path'] == '/items/comic/progress'),
        isTrue,
      );
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'swiping from image to text saves the new asset on exit without scrolling',
    (tester) async {
      final api = ReaderApi();
      final data = book(id: 'mixed', image: true, count: 2);
      (data['assets'] as List)[1]['mime_type'] = 'text/plain';
      await phone(
        tester,
        Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () => Navigator.push(
                context,
                MaterialPageRoute<void>(
                  builder: (_) =>
                      ReaderScreen(api: api, item: LibraryItem.fromJson(data)),
                ),
              ),
              child: const Text('进入混合正文'),
            ),
          ),
        ),
      );
      await tester.tap(find.text('进入混合正文'));
      await tester.pumpAndSettle();
      await tester.pump(const Duration(seconds: 1));
      await tester.pumpAndSettle();
      expect(api.saves.last['page'], 0);
      api.saves.clear();

      await tester.drag(find.byType(PageView), const Offset(-600, 0));
      await tester.pumpAndSettle();
      expect(find.text('第 1 / 1 节'), findsOneWidget);
      // No text scrolling or explicit completion: leaving must flush the asset change.
      await tester.tap(find.byTooltip('返回书库'));
      await tester.pumpAndSettle();
      expect(find.byType(ReaderScreen), findsNothing);
      expect(api.saves.last, containsPair('path', '/items/mixed/progress'));
      expect(api.saves.last, containsPair('page', 1));
      expect(api.saves.last, containsPair('section', 0));
      expect(api.saves.last, containsPair('offset', 0.0));
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'series metadata member does not become readable or auto-download',
    (tester) async {
      final api = ReaderApi();
      await phone(
        tester,
        ReaderScreen(api: api, item: LibraryItem.fromJson(book(series: true))),
      );
      await tester.tap(find.byTooltip('系列与上下篇'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('下篇未下载'));
      await tester.pumpAndSettle();
      expect(find.text('正文尚不可阅读，封面不包含正文。'), findsOneWidget);
      expect(api.posts, isEmpty);
      expect(api.gets.where((path) => path.contains('next-0')), isEmpty);
      await tester.tap(find.text('请求服务器下载正文'));
      await tester.pumpAndSettle();
      expect(api.posts, ['/items/next/download']);
      expect(find.textContaining('正文正在服务器下载队列中'), findsOneWidget);
      await tester.tap(find.text('关闭'));
      await tester.pumpAndSettle();
      expect(find.text('作品 novel'), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'detail refresh invalidates series entries for changed id and revised same id',
    (tester) async {
      Map<String, dynamic> series(String title) => {
        'complete': true,
        'entries': [
          {
            'item_id': 'member',
            'title': title,
            'position': 0,
            'content_state': 'metadata',
            'page_count': 0,
          },
        ],
      };
      final api = ReaderApi()..listed = book(series: true);
      api.seriesResponses['/series/s1'] = series('旧系列成员');
      api.seriesResponses['/series/s2'] = series('新系列成员');
      await phone(tester, LibraryScreen(api: api, onSettings: () {}));
      await tester.tap(find.text('作品 novel'));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('上下篇'));
      await tester.tap(find.text('上下篇'));
      await tester.pumpAndSettle();
      expect(find.text('旧系列成员'), findsOneWidget);

      api.listed = {
        ...api.listed,
        'series': [
          {'id': 's2', 'title': '新关联系列', 'revision': 1},
        ],
      };
      await tester.tap(find.byTooltip('刷新作品'));
      await tester.pumpAndSettle();
      expect(find.text('旧系列成员'), findsNothing);
      expect(find.text('新系列成员'), findsOneWidget);
      expect(api.gets, contains('/series/s2'));

      api.seriesResponses['/series/s2'] = series('同系列已更新成员');
      api.listed = {
        ...api.listed,
        'series': [
          {'id': 's2', 'title': '新关联系列', 'revision': 2},
        ],
      };
      await tester.tap(find.byTooltip('刷新作品'));
      await tester.pumpAndSettle();
      expect(find.text('新系列成员'), findsNothing);
      expect(find.text('同系列已更新成员'), findsOneWidget);
      expect(api.gets.where((p) => p == '/series/s2').length, 2);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'library search uses API query and metadata detail offers server download, not reading',
    (tester) async {
      final api = ReaderApi();
      await phone(tester, LibraryScreen(api: api, onSettings: () {}));
      await tester.enterText(find.byType(TextField), '角色');
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pumpAndSettle();
      expect(api.queries.last['q'], '角色');
      await tester.tap(find.text('作品 novel'));
      await tester.pumpAndSettle();
      expect(find.text('开始 / 继续阅读'), findsNothing);
      expect(find.text('请求服务器下载正文'), findsOneWidget);
      await tester.tap(find.text('请求服务器下载正文'));
      await tester.pumpAndSettle();
      expect(api.posts.where((path) => !path.endsWith('/visit')), [
        '/items/novel/download',
      ]);
      expect(find.text('等待服务器下载'), findsWidgets);
      expect(find.byType(ReaderScreen), findsNothing);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );
}
