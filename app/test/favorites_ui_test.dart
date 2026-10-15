import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/api_image.dart';
import 'package:qiye_library/article_body.dart';
import 'package:qiye_library/favorites_screen.dart';
import 'package:qiye_library/item_cover.dart';
import 'package:qiye_library/library_screen.dart';
import 'package:qiye_library/models.dart';
import 'reading_ui_test.dart' show ReaderApi, book, phone;

class FavoriteApi extends ReaderApi {
  final favoriteWrites = <Map<String, dynamic>>[];
  final deleted = <String>[];
  final groups = [
    for (int i = 0; i < 4; i++)
      {
        'id': 'g$i',
        'name': ['想看', '在看', '已看', '推荐'][i],
        'count': 0,
      },
  ];
  bool failFavorite = false;
  FavoriteApi() {
    listed = {
      ...book(id: 'comic', image: true, state: 'metadata'),
      'page_count': 12,
      'source_bookmark_count': 1234,
    };
  }
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) async {
    if (path == '/favorite-groups') {
      return {
        'items': groups,
        'total_favorites': listed['is_favorite'] == true ? 1 : 0,
        'ungrouped_count': 0,
      };
    }
    return super.get(path, query: query);
  }

  @override
  Future<dynamic> put(String path, [Map<String, dynamic>? body]) async {
    if (!path.endsWith('/favorite')) return super.put(path, body);
    favoriteWrites.add({...?body});
    if (failFavorite) throw const ApiFailure('收藏保存失败');
    final result = {
      'id': 'comic',
      'is_favorite': body!['favorite'],
      'favorite_group_ids': body['favorite'] == true
          ? body['group_ids'] ?? []
          : [],
      'user_score': body['score'] ?? 0,
    };
    listed = {...listed, ...result};
    return result;
  }

  @override
  Future<dynamic> post(String path, [Map<String, dynamic>? body]) async {
    if (path == '/favorite-groups') {
      final group = {'id': 'new', 'name': body!['name'] as String, 'count': 0};
      groups.add(group);
      return group;
    }
    if (path.endsWith('/cover-refresh')) {
      posts.add(path);
      listed = {...listed, 'cover_url': '/items/comic/cover'};
      return {'job_id': 'cover'};
    }
    return super.post(path, body);
  }

  @override
  Future<dynamic> delete(String path) async {
    deleted.add(path);
    if (path.startsWith('/favorite-groups/')) {
      groups.removeWhere((group) => path.endsWith('/${group['id']}'));
    }
    return {'ok': true};
  }
}

void main() {
  testWidgets(
    'detail saves multiple groups and score, updates card without reloading the list',
    (tester) async {
      final api = FavoriteApi();
      await phone(tester, LibraryScreen(api: api));
      await tester.tap(find.text('作品 comic'));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('收藏作品'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('想看'));
      await tester.tap(find.text('推荐'));
      await tester.tap(find.byTooltip('4 星'));
      await tester.tap(find.text('保存收藏'));
      await tester.pumpAndSettle();
      expect(api.favoriteWrites.single['group_ids'], containsAll(['g0', 'g3']));
      expect(api.favoriteWrites.single['score'], 4);
      expect(find.byTooltip('管理收藏'), findsOneWidget);
      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(api.gets.where((path) => path == '/items').length, 1);
      expect(find.text('12 P'), findsOneWidget);
      expect(find.text('★ 1234'), findsOneWidget);
      expect(find.text('评分 4/5'), findsOneWidget);
      expect(find.byIcon(Icons.favorite), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'failed favorite write preserves selected groups and rating until successful retry',
    (tester) async {
      final api = FavoriteApi()..failFavorite = true;
      await phone(
        tester,
        Scaffold(
          body: FavoriteEditor(
            api: api,
            item: LibraryItem.fromJson(api.listed),
          ),
        ),
      );
      await tester.tap(find.text('在看'));
      await tester.tap(find.byTooltip('5 星'));
      await tester.tap(find.text('保存收藏'));
      await tester.pumpAndSettle();
      expect(find.text('收藏保存失败'), findsOneWidget);
      expect(
        tester
            .widget<CheckboxListTile>(
              find.widgetWithText(CheckboxListTile, '在看'),
            )
            .value,
        isTrue,
      );
      expect(api.listed['is_favorite'], isNull);
      expect(api.favoriteWrites.single['score'], 5);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'new group dialog and rename dispose safely; group deletion preserves work favorite',
    (tester) async {
      final api = FavoriteApi()
        ..listed = {...book(id: 'comic'), 'is_favorite': true};
      await phone(
        tester,
        FavoriteGroupsScreen(api: api, onOpen: (_, _) async {}),
      );
      expect(find.text('想看'), findsOneWidget);
      expect(find.text('推荐'), findsOneWidget);
      await tester.tap(find.byTooltip('新建收藏分组'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField).last, '珍藏');
      await tester.tap(find.text('保存'));
      await tester.pumpAndSettle();
      expect(find.text('珍藏'), findsOneWidget);
      await tester.tap(find.byTooltip('管理分组 珍藏'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('重命名'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('取消'));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      await tester.tap(find.byTooltip('管理分组 珍藏'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('删除分组'));
      await tester.pumpAndSettle();
      expect(find.text('只删除分组，作品仍保留在我的收藏中。'), findsOneWidget);
      await tester.tap(find.text('删除分组'));
      await tester.pumpAndSettle();
      expect(api.deleted, ['/favorite-groups/new']);
      expect(api.listed['is_favorite'], isTrue);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'favorite and history screens use their server filters, clear history requires confirmation',
    (tester) async {
      final api = FavoriteApi();
      await phone(
        tester,
        LibraryScreen(
          api: api,
          initialFavoriteGroup: 'g0',
          initialFavoriteName: '想看',
        ),
      );
      expect(api.queries.last, containsPair('favorite', 'true'));
      expect(api.queries.last, containsPair('favorite_group', 'g0'));
      await tester.pumpWidget(const SizedBox());
      await phone(tester, LibraryScreen(api: api, initialHistory: true));
      expect(api.queries.last, containsPair('history', 'true'));
      await tester.tap(find.byTooltip('清空浏览历史'));
      await tester.pumpAndSettle();
      expect(api.deleted, isEmpty);
      await tester.tap(find.text('取消'));
      await tester.pumpAndSettle();
      expect(api.deleted, isEmpty);
      await tester.tap(find.byTooltip('清空浏览历史'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('清空历史'));
      await tester.pumpAndSettle();
      expect(api.deleted, ['/history']);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'novel covers display independently from body; cover queue refresh does not request body',
    (tester) async {
      final api = FavoriteApi()..listed = book(id: 'comic', state: 'metadata');
      await phone(tester, LibraryScreen(api: api));
      await tester.tap(find.text('作品 comic'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('获取小说封面'));
      await tester.pumpAndSettle();
      await tester.pump(const Duration(seconds: 6));
      await tester.pumpAndSettle();
      expect(
        find.byWidgetPredicate(
          (w) => w is ApiImage && w.path == '/items/comic/cover',
        ),
        findsOneWidget,
      );
      expect(api.posts.where((path) => path.endsWith('/download')), isEmpty);
      expect(api.posts.where((path) => path.endsWith('/visit')).length, 1);
      expect(find.text('开始 / 继续阅读'), findsNothing);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'unknown upstream count is hidden, actual zero is shown, novel uses its image cover',
    (tester) async {
      final api = FavoriteApi();
      await phone(
        tester,
        Scaffold(
          body: SizedBox(
            width: 180,
            height: 240,
            child: ItemCover(
              api: api,
              item: LibraryItem.fromJson({
                ...api.listed,
                'source_bookmark_count': null,
              }),
            ),
          ),
        ),
      );
      expect(find.textContaining('★'), findsNothing);
      await tester.pumpWidget(const SizedBox());
      await phone(
        tester,
        Scaffold(
          body: SizedBox(
            width: 180,
            height: 240,
            child: ItemCover(
              api: api,
              item: LibraryItem.fromJson({
                ...api.listed,
                'source_bookmark_count': 0,
                'type': 'novel',
                'cover_url': '/items/comic/cover',
              }),
            ),
          ),
        ),
      );
      expect(find.text('★ 0'), findsOneWidget);
      expect(find.text('12 P'), findsNothing);
      expect(find.byType(ApiImage), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'paragraph spacing is distinct from line spacing and preserves indentation',
    (tester) async {
      await phone(
        tester,
        const Scaffold(
          body: SelectionArea(
            child: ArticleBody(text: '　　第一段\r\n第二段', fontSize: 20),
          ),
        ),
      );
      final first = find.text('　　第一段'), second = find.text('第二段');
      expect(
        tester.getTopLeft(second).dy - tester.getBottomLeft(first).dy,
        closeTo(18, .1),
      );
      expect(tester.widget<Text>(first).style!.height, 1.85);
      expect(find.byType(SelectionArea), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );
}
