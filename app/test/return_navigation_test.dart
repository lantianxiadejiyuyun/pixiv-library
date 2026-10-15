import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/characters_screen.dart';
import 'package:qiye_library/library_screen.dart';
import 'package:qiye_library/models.dart';
import 'package:qiye_library/reader_screen.dart';
import 'reading_ui_test.dart' show ReaderApi, book, phone;
import 'character_wall_test.dart' show CharacterApi;

class ReturnApi extends ReaderApi {
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) async {
    if (path == '/items') {
      gets.add(path);
      queries.add({...?query});
      final page = int.parse(query?['page'] ?? '1');
      return {
        'items': [
          for (int i = 0; i < 24; i++)
            book(id: 'book-${(page - 1) * 24 + i}', state: 'metadata'),
        ],
        'total': 48,
      };
    }
    return super.get(path, query: query);
  }
}

Finder downScroll() => find
    .byWidgetPredicate(
      (widget) =>
          widget is Scrollable && widget.axisDirection == AxisDirection.down,
    )
    .first;

void main() {
  testWidgets('only successful progress writes notify cached pages', (
    tester,
  ) async {
    final api = ReaderApi()..failProgress = true;
    final saved = <ReadingPosition>[];
    await phone(
      tester,
      ReaderScreen(
        api: api,
        item: LibraryItem.fromJson(book(image: true)),
        onProgressSaved: (id, position) => saved.add(position),
      ),
    );
    await tester.pump(const Duration(seconds: 1));
    await tester.pumpAndSettle();
    expect(saved, isEmpty);
    api.failProgress = false;
    await tester.tap(find.text('重试'));
    await tester.pumpAndSettle();
    expect(saved.single.page, 0);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
  });
  testWidgets(
    'back from detail keeps accumulated pages, exact scroll and filters without list request',
    (tester) async {
      final api = ReturnApi()..listed = book(id: 'book-30', state: 'metadata');
      await phone(
        tester,
        LibraryScreen(api: api, initialSource: 'pixiv', initialAuthor: '某作者'),
      );
      var scroll = tester.state<ScrollableState>(downScroll());
      scroll.position.jumpTo(scroll.position.maxScrollExtent);
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
        find.text('作品 book-30'),
        250,
        scrollable: downScroll(),
      );
      await tester.pumpAndSettle();
      scroll = tester.state<ScrollableState>(downScroll());
      final offset = scroll.position.pixels;
      final count = api.gets.where((path) => path == '/items').length;
      await tester.tap(find.text('作品 book-30'));
      await tester.pumpAndSettle();
      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(api.gets.where((path) => path == '/items').length, count);
      expect(
        (tester.widget<SliverGrid>(find.byType(SliverGrid)).delegate
                as SliverChildBuilderDelegate)
            .childCount,
        48,
      );
      expect(
        tester.state<ScrollableState>(downScroll()).position.pixels,
        offset,
      );
      expect(
        api.queries.every(
          (q) => q['source'] == 'pixiv' && q['author'] == '某作者',
        ),
        isTrue,
      );
      // Pull to refresh is still explicit and replaces the accumulated pages.
      await tester
          .widget<RefreshIndicator>(find.byType(RefreshIndicator))
          .onRefresh();
      await tester.pumpAndSettle();
      expect(api.gets.where((path) => path == '/items').length, count + 1);
      expect(
        (tester.widget<SliverGrid>(find.byType(SliverGrid)).delegate
                as SliverChildBuilderDelegate)
            .childCount,
        24,
      );
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'return from reading makes no detail request and resumes last successfully saved page',
    (tester) async {
      final api = ReaderApi()
        ..listed = book(id: 'comic', image: true, count: 3);
      await phone(tester, LibraryScreen(api: api));
      await tester.tap(find.text('作品 comic'));
      await tester.pumpAndSettle();
      final count = api.gets.length;
      await tester.tap(find.text('开始 / 继续阅读'));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('下一 P'));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('返回书库'));
      await tester.pumpAndSettle();
      expect(api.gets.length, count);
      expect(api.saves.last['page'], 1);
      await tester.tap(find.text('开始 / 继续阅读'));
      await tester.pumpAndSettle();
      expect(find.text('第 2 / 3 P'), findsOneWidget);
      expect(find.byType(ReaderScreen), findsOneWidget);
      expect(api.gets.length, count);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'return from character keeps wall and does not refetch its first page',
    (tester) async {
      final api = CharacterApi();
      await phone(tester, CharactersScreen(api: api, onOpenItem: (_) async {}));
      final count = api.reads.where((q) => q['path'] == '/characters').length;
      await tester.tap(find.text('人物 0'));
      await tester.pumpAndSettle();
      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(api.reads.where((q) => q['path'] == '/characters').length, count);
      expect(find.text('人物 0'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );
}
