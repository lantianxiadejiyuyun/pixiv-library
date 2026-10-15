import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/library_screen.dart';
import 'package:qiye_library/author_screen.dart';
import 'package:qiye_library/tag_browser_screen.dart';

Map<String, dynamic> book(int id) => {
  'id': '$id',
  'title': 'Book $id',
  'author': 'Author',
  'source': 'pixiv',
  'type': 'manga',
  'content_state': 'metadata',
};

class PagedApi extends LibraryApi {
  final calls = <Map<String, String>>[];
  Completer<dynamic>? second;
  bool failSecond = false;
  PagedApi() : super(server: Uri.parse('https://library.test'));
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) async {
    calls.add({'path': path, ...?query});
    final page = int.parse(query?['page'] ?? '1');
    if (path == '/items') {
      if (page == 2 && failSecond) throw const ApiFailure('失败');
      if (page == 2 && second != null) return second!.future;
      return {
        'items': List.generate(24, (i) => book((page - 1) * 24 + i)),
        'total': 48,
      };
    }
    if (path == '/tags') {
      final offset = int.parse(query?['offset'] ?? '0');
      return List.generate(
        offset == 0 ? 100 : 2,
        (i) => {'id': '${offset + i}', 'name': 'Tag ${offset + i}', 'count': 1},
      );
    }
    if (path == '/pixiv/authors') {
      return {
        'items': List.generate(
          30,
          (i) => {
            'remote_id': '${(page - 1) * 30 + i}',
            'name': 'Author ${(page - 1) * 30 + i}',
          },
        ),
        'total': 60,
      };
    }
    if (path == '/pixiv/authors/10') {
      return {
        'remote_id': '10',
        'name': 'Author',
        'description': '',
        'can_read_private': false,
      };
    }
    if (path == '/pixiv/tasks/profile') {
      return {'status': 'completed', 'author_id': '10'};
    }
    if (path.endsWith('/feed')) {
      return {
        'items': List.generate(24, (i) => book((page - 1) * 24 + i)),
        'total': 48,
        'has_more': false,
        'revision': 1,
        'generation': 1,
        'unavailable': 0,
        'complete': true,
        'job': null,
      };
    }
    throw const ApiFailure('未知');
  }

  @override
  Future<dynamic> post(String path, [Map<String, dynamic>? body]) async => {
    'remote_id': '10',
    'section': 'works',
    'category': 'illustration',
    'restrict': 'public',
    'job': {'id': 'profile'},
  };
}

void bottom(WidgetTester tester) {
  final state = tester.state<ScrollableState>(
    find
        .byWidgetPredicate(
          (widget) =>
              widget is Scrollable &&
              widget.axisDirection == AxisDirection.down,
        )
        .first,
  );
  state.position.jumpTo(state.position.maxScrollExtent);
}

void main() {
  testWidgets(
    'library appends one page, retains items on error and retries same page',
    (tester) async {
      final api = PagedApi()..failSecond = true;
      await tester.pumpWidget(MaterialApp(home: LibraryScreen(api: api)));
      await tester.pumpAndSettle();
      bottom(tester);
      await tester.pumpAndSettle();
      expect(
        (tester.widget<SliverGrid>(find.byType(SliverGrid)).delegate
                as SliverChildBuilderDelegate)
            .childCount,
        24,
      );
      expect(find.text('加载失败，重试'), findsOneWidget);
      api.failSecond = false;
      api.second = Completer<dynamic>();
      await tester.tap(find.text('加载失败，重试'));
      await tester.pump();
      bottom(tester);
      await tester.pump();
      expect(api.calls.where((c) => c['page'] == '2').length, 2);
      api.second!.complete({
        'items': List.generate(24, (i) => book(24 + i)),
        'total': 48,
      });
      await tester.pumpAndSettle();
      expect(
        (tester.widget<SliverGrid>(find.byType(SliverGrid)).delegate
                as SliverChildBuilderDelegate)
            .childCount,
        48,
      );
      expect(find.text('已全部加载'), findsNothing);
      expect(find.textContaining('已加载'), findsNothing);
      bottom(tester);
      await tester.pumpAndSettle();
      expect(api.calls.where((c) => c['page'] == '2').length, 2);
    },
  );
  testWidgets('refresh replaces accumulated pages from page one', (
    tester,
  ) async {
    final api = PagedApi();
    await tester.pumpWidget(MaterialApp(home: LibraryScreen(api: api)));
    await tester.pumpAndSettle();
    bottom(tester);
    await tester.pumpAndSettle();
    final refresh = tester.widget<RefreshIndicator>(
      find.byType(RefreshIndicator),
    );
    final future = refresh.onRefresh();
    await tester.pumpAndSettle();
    await future;
    expect(api.calls.last['page'], '1');
    expect(
      (tester.widget<SliverGrid>(find.byType(SliverGrid)).delegate
              as SliverChildBuilderDelegate)
          .childCount,
      24,
    );
  });
  testWidgets('author directory and author feed append when scrolled down', (
    tester,
  ) async {
    final api = PagedApi();
    await tester.pumpWidget(
      MaterialApp(
        home: AuthorsScreen(api: api, onOpenItem: (_) {}),
      ),
    );
    await tester.pumpAndSettle();
    bottom(tester);
    await tester.pumpAndSettle();
    expect(
      tester
          .widget<ListView>(find.byType(ListView))
          .childrenDelegate
          .estimatedChildCount,
      60,
    );
    expect(find.textContaining('已加载'), findsNothing);
    await tester.pumpWidget(const SizedBox());
    await tester.pumpWidget(
      MaterialApp(
        home: AuthorScreen(api: api, target: '10', onOpenItem: (_) {}),
      ),
    );
    await tester.pumpAndSettle();
    bottom(tester);
    await tester.pumpAndSettle();
    expect(
      tester
          .widget<ListView>(find.byType(ListView))
          .childrenDelegate
          .estimatedChildCount,
      48,
    );
    expect(
      api.calls
          .where((c) => c['path']!.endsWith('/feed') && c['page'] == '2')
          .length,
      1,
    );
  });
  testWidgets('tags append automatically and stop at the last batch', (
    tester,
  ) async {
    final api = PagedApi();
    await tester.pumpWidget(MaterialApp(home: TagBrowserScreen(api: api)));
    await tester.pumpAndSettle();
    bottom(tester);
    await tester.pumpAndSettle();
    expect(api.calls.where((c) => c['offset'] == '100').length, 1);
    bottom(tester);
    await tester.pumpAndSettle();
    expect(api.calls.where((c) => c['path'] == '/tags').length, 2);
  });
  testWidgets(
    'downloaded category keeps filtering during search, pagination and platform selection',
    (tester) async {
      final api = PagedApi();
      await tester.pumpWidget(MaterialApp(home: LibraryScreen(api: api)));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('打开侧边栏'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(ListTile, '已下载'));
      await tester.pumpAndSettle();
      expect(api.calls.last['downloaded'], 'true');
      expect(
        find.descendant(
          of: find.byType(AppBar),
          matching: find.byType(TextField),
        ),
        findsOneWidget,
      );
      expect(find.textContaining('已加载'), findsNothing);
      await tester.enterText(find.byType(TextField), 'comic');
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pumpAndSettle();
      expect(api.calls.last['q'], 'comic');
      expect(api.calls.last['downloaded'], 'true');
      bottom(tester);
      await tester.pumpAndSettle();
      expect(api.calls.last['page'], '2');
      expect(api.calls.last['downloaded'], 'true');
      await tester.tap(find.byTooltip('打开侧边栏'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(ListTile, 'Pixiv'));
      await tester.pumpAndSettle();
      expect(api.calls.last['source'], 'pixiv');
      expect(api.calls.last['downloaded'], 'true');
      await tester.tap(find.byTooltip('打开侧边栏'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(ListTile, '全部书库'));
      await tester.pumpAndSettle();
      expect(api.calls.last.containsKey('downloaded'), false);
    },
  );
}
