import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/library_screen.dart';

class MetadataApi extends LibraryApi {
  MetadataApi() : super(server: Uri.parse('https://library.test'));
  final queries = <Map<String, String>>[];
  final translations = <Map<String, dynamic>>[];
  bool pending = false, translated = false, unavailable = false;
  int polls = 0;
  Map<String, dynamic> get book => {
    'id': 'book1',
    'title': 'Original title',
    'title_zh': translated ? '安静的午后' : null,
    'source': 'pixiv',
    'type': 'manga',
    'author': 'Exact Author',
    'content_state': 'metadata',
    'tags': [
      {'id': 'tag-id', 'name': 'blue_eyes', 'name_zh': '蓝眼睛'},
    ],
    'assets': [],
  };
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) async {
    if (path == '/items') {
      queries.add({...?query});
      return {
        'items': [book],
        'total': 1,
      };
    }
    if (path == '/tags') {
      queries.add({...?query});
      return [
        {'id': 'tag-id', 'name': 'blue_eyes', 'name_zh': '蓝眼睛', 'count': 4},
      ];
    }
    if (path.endsWith('/translation')) {
      if (pending && polls++ > 0) {
        pending = false;
        translated = true;
      }
      return {
        'status': pending
            ? 'queued'
            : translated
            ? 'completed'
            : 'idle',
      };
    }
    if (path == '/items/book1') return book;
    throw const ApiFailure('不存在', 404);
  }

  @override
  Future<dynamic> post(String path, [Map<String, dynamic>? body]) async {
    if (path.endsWith('/visit')) return {'ok': true};
    if (unavailable) throw const ApiFailure('未配置', 503);
    translations.add({...?body});
    pending = true;
    return {'status': 'queued', 'job_id': 'job1'};
  }
}

Future<void> reveal(WidgetTester tester, Finder target) async {
  await tester.scrollUntilVisible(
    target,
    160,
    scrollable: find.byType(Scrollable).first,
  );
  await tester.pumpAndSettle();
}

Future<void> openDetail(WidgetTester tester, MetadataApi api) async {
  await tester.pumpWidget(MaterialApp(home: LibraryScreen(api: api)));
  await tester.pumpAndSettle();
  await tester.tap(find.text(api.translated ? '安静的午后' : 'Original title'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets(
    'tag search uses Chinese text then filters by stable tag ID and source',
    (tester) async {
      final api = MetadataApi();
      await tester.pumpWidget(
        MaterialApp(
          home: LibraryScreen(api: api, initialSource: 'pixiv'),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('打开侧边栏'));
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
        find.widgetWithText(ListTile, '搜索标签'),
        250,
        scrollable: find
            .descendant(
              of: find.byType(Drawer),
              matching: find.byType(Scrollable),
            )
            .first,
      );
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(ListTile, '搜索标签'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), '蓝眼睛');
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pumpAndSettle();
      expect(api.queries.last['q'], '蓝眼睛');
      expect(api.queries.last['source'], 'pixiv');
      expect(find.text('blue_eyes · 4 部作品'), findsOneWidget);
      await tester.tap(find.widgetWithText(ListTile, '蓝眼睛'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('查看作品'));
      await tester.pumpAndSettle();
      expect(api.queries.last['tag'], 'tag-id');
      expect(api.queries.last['source'], 'pixiv');
      expect(api.queries.last.containsKey('q'), false);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'content tag opens tag works and translated title preserves original',
    (tester) async {
      final api = MetadataApi()..translated = true;
      await openDetail(tester, api);
      expect(find.text('原题：Original title'), findsOneWidget);
      final tag = find.widgetWithText(ActionChip, '蓝眼睛');
      await reveal(tester, tag);
      await tester.tap(tag);
      await tester.pumpAndSettle();
      expect(api.queries.last['tag'], 'tag-id');
      expect(api.queries.last['source'], 'pixiv');
      expect(find.textContaining('标签作品：'), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'same author navigation sends exact author and platform filters',
    (tester) async {
      final api = MetadataApi();
      await openDetail(tester, api);
      await tester.tap(find.text('查看同作者作品'));
      await tester.pumpAndSettle();
      expect(api.queries.last['author'], 'Exact Author');
      expect(api.queries.last['source'], 'pixiv');
      expect(api.queries.last.containsKey('q'), false);
      expect(find.textContaining('同作者作品：'), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'translation requires confirmation, queues in background and refreshes display',
    (tester) async {
      final api = MetadataApi();
      await openDetail(tester, api);
      await reveal(tester, find.text('AI 汉化标题和标签'));
      await tester.tap(find.text('AI 汉化标题和标签'));
      await tester.pumpAndSettle();
      expect(api.translations, isEmpty);
      await tester.tap(find.text('开始汉化'));
      await tester.pumpAndSettle();
      expect(api.translations.single, {'consent': true, 'force': false});
      expect(find.text('后台汉化中…'), findsOneWidget);
      await tester.pump(const Duration(seconds: 3));
      await tester.pumpAndSettle();
      expect(find.text('安静的午后'), findsOneWidget);
      expect(find.text('原题：Original title'), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets('unconfigured model shows actionable server settings message', (
    tester,
  ) async {
    final api = MetadataApi()..unavailable = true;
    await openDetail(tester, api);
    await reveal(tester, find.text('AI 汉化标题和标签'));
    await tester.tap(find.text('AI 汉化标题和标签'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('开始汉化'));
    await tester.pumpAndSettle();
    expect(find.text('请先在网页设置中配置并启用 AI 汉化模型。'), findsOneWidget);
    expect(find.text('后台汉化中…'), findsNothing);
    await tester.pumpWidget(const SizedBox());
  });
}
