import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/characters_screen.dart';
import 'package:qiye_library/models.dart';

Map<String, dynamic> person(int index, {bool profile = true}) => {
  'id': 'role-$index',
  'name': 'hero_$index',
  'display_name': '人物 $index',
  'tag_name': '角色:hero_$index',
  'count': 1,
  'covers': [],
  'profile': profile
      ? {
          'id': '17',
          'name': 'Hero',
          'native_name': '人物 $index',
          'aliases': ['英雄'],
          'description': '已保存的人物介绍',
          'provider': 'AniList',
          'source_url': 'https://anilist.co/character/17',
          'works': [
            {
              'title': '原作样本',
              'type': 'manga',
              'source_url': 'https://anilist.co/manga/1',
            },
          ],
        }
      : null,
};

class CharacterApi extends LibraryApi {
  CharacterApi() : super(server: Uri.parse('https://library.test'));
  final reads = <Map<String, String>>[], writes = <Map<String, dynamic>>[];
  Completer<dynamic>? slow;
  bool failMore = false, failSave = false;
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) async {
    reads.add({'path': path, ...?query});
    if (path == '/characters') {
      if (query?['q'] == 'slow' && slow != null) return slow!.future;
      final page = int.parse(query?['page'] ?? '1');
      if (page == 2 && failMore) throw const ApiFailure('第二页失败');
      return {
        'items': List.generate(
          page == 1 ? 24 : 1,
          (i) => person((page - 1) * 24 + i),
        ),
        'total': 25,
      };
    }
    if (path.startsWith('/characters/')) return person(0);
    if (path == '/items') {
      return {
        'items': [
          {
            'id': 'work',
            'title': '人物对应漫画',
            'source': 'pixiv',
            'type': 'manga',
            'content_state': 'metadata',
          },
        ],
        'total': 1,
      };
    }
    throw const ApiFailure('不存在', 404);
  }

  @override
  Future<dynamic> post(String path, [Map<String, dynamic>? body]) async {
    writes.add({'path': path, ...?body});
    if (path == '/characters/search') {
      return {
        'candidates': [
          {
            'id': '17',
            'name': 'Hero',
            'native_name': '人物候选',
            'aliases': ['别名'],
            'description': '网络介绍候选',
            'works': [],
            'source_url': 'https://anilist.co/character/17',
          },
        ],
      };
    }
    if (failSave) throw const ApiFailure('保存失败');
    return person(0);
  }
}

Future<void> showWall(
  WidgetTester tester,
  CharacterApi api, {
  WallCharacter? character,
  Future<void> Function(String)? onOpen,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: CharactersScreen(
        api: api,
        character: character,
        onOpenItem: onOpen ?? (_) => Future.value(),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('人物墙下滑加载并保留首屏，来源与已下载筛选传给服务器', (tester) async {
    final api = CharacterApi();
    await showWall(tester, api);
    expect(find.text('人物 0'), findsOneWidget);
    await tester.drag(find.byType(CustomScrollView), const Offset(0, -7000));
    await tester.pumpAndSettle();
    expect(api.reads.any((r) => r['page'] == '2'), isTrue);
    expect(find.text('人物 24'), findsOneWidget);
    await tester.drag(find.byType(CustomScrollView), const Offset(0, 7000));
    await tester.pumpAndSettle();
    expect(find.text('人物 0'), findsOneWidget);
    await tester.tap(find.text('仅已下载'));
    await tester.pumpAndSettle();
    expect(api.reads.last['downloaded'], 'true');
    expect(api.reads.last['page'], '1');
    expect(api.writes, isEmpty);
  });
  testWidgets('人物资料离线读取，点击本地作品跳转现有阅读详情', (tester) async {
    final api = CharacterApi();
    String? opened;
    await showWall(
      tester,
      api,
      character: WallCharacter.fromJson(person(0)),
      onOpen: (id) async => opened = id,
    );
    expect(find.text('已保存的人物介绍'), findsOneWidget);
    expect(find.text('原作：原作样本'), findsOneWidget);
    expect(api.writes, isEmpty);
    expect(api.reads.any((r) => r['tag'] == 'role-0'), isTrue);
    await tester.scrollUntilVisible(
      find.text('人物对应漫画'),
      350,
      scrollable: find
          .descendant(
            of: find.byType(CustomScrollView),
            matching: find.byType(Scrollable),
          )
          .first,
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('人物对应漫画'));
    await tester.pumpAndSettle();
    expect(opened, 'work');
  });
  testWidgets('旧搜索响应不覆盖新结果，离开页面后不更新状态', (tester) async {
    final api = CharacterApi()..slow = Completer<dynamic>();
    await showWall(tester, api);
    await tester.enterText(find.byType(TextField), 'slow');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.enterText(find.byType(TextField), 'new');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pumpAndSettle();
    api.slow!.complete({
      'items': [person(99)],
      'total': 1,
    });
    await tester.pumpAndSettle();
    expect(find.text('人物 99'), findsNothing);
    expect(find.text('人物 0'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
  });
  testWidgets('联网查询不自动绑定，明确确认后仅提交人物 ID', (tester) async {
    final api = CharacterApi();
    await tester.pumpWidget(
      MaterialApp(home: CharacterResearchScreen(api: api)),
    );
    await tester.pumpAndSettle();
    expect(api.writes, isEmpty);
    await tester.enterText(find.byType(TextField), 'Hero');
    await tester.tap(find.text('搜索人物'));
    await tester.pumpAndSettle();
    expect(find.text('网络介绍候选'), findsOneWidget);
    expect(api.writes.length, 1);
    await tester.scrollUntilVisible(
      find.text('确认此人物并保存'),
      300,
      scrollable: find.byType(Scrollable).last,
    );
    await tester.tap(find.text('确认此人物并保存'));
    await tester.pumpAndSettle();
    expect(api.writes.last, {'path': '/characters', 'candidate_id': 17});
  });
  testWidgets('联网保存失败不丢候选且允许重试', (tester) async {
    final api = CharacterApi()..failSave = true;
    await tester.pumpWidget(
      MaterialApp(
        home: CharacterResearchScreen(
          api: api,
          character: WallCharacter.fromJson(person(0)),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('搜索人物'));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.text('确认此人物并保存'),
      300,
      scrollable: find.byType(Scrollable).last,
    );
    await tester.tap(find.text('确认此人物并保存'));
    await tester.pumpAndSettle();
    expect(find.text('保存失败'), findsOneWidget);
    expect(find.text('人物候选'), findsOneWidget);
    expect(api.writes.last['path'], '/characters/role-0/profile');
  });
}
