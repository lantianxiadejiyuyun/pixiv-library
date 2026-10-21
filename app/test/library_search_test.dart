import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/library_search.dart';
import 'package:qiye_library/library_screen.dart';
import 'package:qiye_library/models.dart';

const tags = [
  {'id': 'blue', 'name': 'blue_eyes', 'name_zh': '蓝眼睛', 'count': 4},
  {'id': 'red', 'name': 'red_hair', 'name_zh': '红头发', 'count': 3},
];

class SearchApi extends LibraryApi {
  SearchApi([String origin = 'https://library.test'])
    : super(server: Uri.parse(origin));
  final itemQueries = <Map<String, String>>[],
      tagQueries = <Map<String, String>>[];
  final pending = <Completer<dynamic>>[];
  bool delay = false, fail = false;
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) async {
    if (path == '/tags') {
      tagQueries.add({...?query});
      if (fail) throw const ApiFailure('offline', 503);
      if (delay) {
        final completer = Completer<dynamic>();
        pending.add(completer);
        return completer.future;
      }
      return tags;
    }
    if (path == '/items') {
      itemQueries.add({...?query});
      return {'items': [], 'total': 0};
    }
    throw const ApiFailure('missing', 404);
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() => FlutterSecureStorage.setMockInitialValues({}));

  testWidgets('home search fits a phone with keyboard and active filters', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(360, 640);
    tester.view.devicePixelRatio = 1;
    tester.view.viewInsets = const FakeViewPadding(bottom: 280);
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetViewInsets);
    await tester.pumpWidget(
      MaterialApp(
        home: LibraryScreen(
          api: SearchApi(),
          initialSource: 'pixiv',
          initialTag: LibraryTag.fromJson(tags[0]),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), '红');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pumpAndSettle();
    expect(find.text('标签联想'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
  });

  test(
    'search history persists, deduplicates tag order and separates servers',
    () async {
      final controller = LibrarySearchController(SearchApi());
      await controller.ready;
      final selected = tags.map(LibraryTag.fromJson).toList();
      for (var i = 0; i < 25; i++) {
        controller.remember(SearchEntry('$i', const []));
      }
      controller.remember(SearchEntry('', selected, 'any'));
      controller.remember(SearchEntry('', selected.reversed.toList(), 'any'));
      expect(controller.history.length, 20);
      expect(controller.history.where((e) => e.tags.isNotEmpty).length, 1);
      await controller.saved;
      final restored = LibrarySearchController(SearchApi());
      await restored.ready;
      expect(restored.history.first.mode, 'any');
      expect(restored.history.first.label, contains('蓝眼睛'));
      final other = LibrarySearchController(SearchApi('https://other.test'));
      await other.ready;
      expect(other.history, isEmpty);
      restored.remove(restored.history.first);
      await restored.saved;
      expect(restored.history.length, 19);
      restored.remove();
      await restored.saved;
      expect(restored.history, isEmpty);
      controller.dispose();
      restored.dispose();
      other.dispose();
    },
  );

  test(
    'tag suggestions discard stale results, exclude selections and survive errors',
    () async {
      final api = SearchApi()..delay = true;
      final controller = LibrarySearchController(api);
      await controller.ready;
      controller.show(true);
      controller.update('blue', 'pixiv', {});
      final old = controller.suggest();
      controller.update('红', 'pixiv', {'blue'});
      final current = controller.suggest();
      api.pending[1].complete(tags);
      await current;
      api.pending[0].complete([
        {'id': 'stale', 'name': 'old'},
      ]);
      await old;
      expect(controller.suggestions.single.id, 'red');
      expect(api.tagQueries.last['source'], 'pixiv');
      api.fail = true;
      await controller.suggest();
      expect(controller.error, 'offline');
      api.fail = false;
      final late = controller.suggest();
      controller.dispose();
      api.pending.last.complete(tags);
      await late;
    },
  );

  testWidgets(
    'home suggestions select a tag; multi-select applies any/all and survives history restore',
    (tester) async {
      final api = SearchApi();
      await tester.pumpWidget(
        MaterialApp(
          home: LibraryScreen(api: api, initialSource: 'pixiv'),
        ),
      );
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), '蓝');
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pumpAndSettle();
      expect(find.text('标签联想'), findsOneWidget);
      await tester.tap(find.widgetWithText(ListTile, '蓝眼睛'));
      await tester.pumpAndSettle();
      expect(api.itemQueries.last['tag'], 'blue');
      expect(api.itemQueries.last.containsKey('q'), false);
      await tester.tap(find.text('编辑标签'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(ListTile, '红头发'));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(DropdownButton<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('匹配任一').last);
      await tester.pumpAndSettle();
      await tester.tap(find.text('查看作品'));
      await tester.pumpAndSettle();
      expect(api.itemQueries.last['tag_ids'], 'blue,red');
      expect(api.itemQueries.last['tag_mode'], 'any');
      expect(api.itemQueries.last['source'], 'pixiv');
      await tester.tap(find.byType(TextField));
      await tester.pumpAndSettle();
      expect(find.text('搜索历史'), findsOneWidget);
      await tester.tap(find.widgetWithText(ListTile, '#蓝眼睛 · #红头发'));
      await tester.pumpAndSettle();
      expect(api.itemQueries.last['tag_ids'], 'blue,red');
      expect(api.itemQueries.last['tag_mode'], 'any');
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'keyword history survives rebuilding the home page and can be cleared',
    (tester) async {
      final api = SearchApi();
      await tester.pumpWidget(MaterialApp(home: LibraryScreen(api: api)));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), '海边');
      await tester.testTextInput.receiveAction(TextInputAction.search);
      await tester.pumpAndSettle();
      expect(api.itemQueries.last['q'], '海边');
      await tester.pumpWidget(const SizedBox());
      await tester.pumpAndSettle();
      await tester.pumpWidget(MaterialApp(home: LibraryScreen(api: api)));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(TextField));
      await tester.pumpAndSettle();
      expect(find.widgetWithText(ListTile, '海边'), findsOneWidget);
      await tester.tap(find.widgetWithText(ListTile, '海边'));
      await tester.pumpAndSettle();
      expect(api.itemQueries.last['q'], '海边');
      await tester.tap(find.byTooltip('清空搜索'));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(TextField));
      await tester.pumpAndSettle();
      await tester.tap(find.text('清空历史'));
      await tester.pumpAndSettle();
      expect(find.text('暂无搜索历史'), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
    },
  );
}
