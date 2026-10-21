import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/author_screen.dart';
import 'package:qiye_library/item_cover.dart';
import 'author_ui_test.dart' show AuthorApi;

class StyledAuthorApi extends AuthorApi {
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) async {
    final result = await super.get(path, query: query);
    if (path == '/pixiv/authors/10') {
      return {
        ...result as Map<String, dynamic>,
        'name': '一位名字较长的插画与漫画创作者',
        'description': List.filled(10, '分享旅途中的风景，也记录平凡生活里的小故事。').join('\n'),
        'download_settings': {
          'download_works': false,
          'download_bookmarks': true,
        },
      };
    }
    return result;
  }

  @override
  Map<String, dynamic> feed(String category) => {
    ...super.feed(category),
    'items': [
      {
        'id': category,
        'title': '$category work',
        'author': '合成作者',
        'type': category,
        'source': 'pixiv',
        'content_state': 'ready',
        'page_count': 12,
        'source_bookmark_count': 1830,
      },
    ],
  };
}

Future<void> render(
  WidgetTester tester, {
  double width = 390,
  double scale = 1,
  Brightness brightness = Brightness.light,
}) async {
  tester.view.physicalSize = Size(width, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final api = StyledAuthorApi();
  addTearDown(api.close);
  await tester.pumpWidget(
    MaterialApp(
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xff5967aa),
          brightness: brightness,
        ),
      ),
      builder: (context, child) => MediaQuery(
        data: MediaQuery.of(
          context,
        ).copyWith(textScaler: TextScaler.linear(scale)),
        child: child!,
      ),
      home: AuthorScreen(api: api, target: '10', onOpenItem: (_) {}),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets(
    'author page presents cover cards and keeps download controls folded',
    (tester) async {
      await render(tester);
      expect(find.text('作者详情'), findsOneWidget);
      expect(find.text('自动下载设置'), findsOneWidget);
      expect(find.text('下载作者作品'), findsNothing);
      expect(find.byType(SliverGrid), findsOneWidget);
      expect(find.byType(ItemCover), findsWidgets);
      await tester.scrollUntilVisible(find.text('12 P'), 120);
      expect(find.text('★ 1830'), findsOneWidget);
      expect(find.text('漫画 · 已下载'), findsNothing);
      expect(find.text('插画 · 已下载'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'biography expands and settings can be opened without replacing filters',
    (tester) async {
      await render(tester);
      await tester.tap(find.text('展开简介'));
      await tester.pumpAndSettle();
      expect(find.text('收起简介'), findsOneWidget);
      final description = tester.widget<Text>(find.textContaining('分享旅途中的风景'));
      expect(description.maxLines, isNull);
      await tester.ensureVisible(find.text('收起简介'));
      await tester.tap(find.text('收起简介'));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('自动下载设置'));
      await tester.tap(find.text('自动下载设置'));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('下载作者作品'));
      expect(find.text('下载作者公开收藏'), findsOneWidget);
      expect(find.text('作者作品'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'small screen with large text scrolls profile and uses one column',
    (tester) async {
      await render(tester, width: 320, scale: 1.8, brightness: Brightness.dark);
      await tester.scrollUntilVisible(find.text('★ 1830'), 160);
      final grid = tester.widget<SliverGrid>(find.byType(SliverGrid));
      expect(
        (grid.gridDelegate as SliverGridDelegateWithFixedCrossAxisCount)
            .crossAxisCount,
        1,
      );
      expect(find.text('插画 · 已下载'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );
}
