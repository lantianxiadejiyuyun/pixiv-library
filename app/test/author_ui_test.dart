import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/author_screen.dart';

class AuthorApi extends LibraryApi {
  AuthorApi() : super(server: Uri.parse('https://library.test'));
  final filters = <Map<String, String>>[];
  final posts = <Map<String, dynamic>>[];
  bool privateAllowed = false,
      unavailable = false,
      profilePending = false,
      empty = false;
  int feedPoll = 0;
  Completer<Map<String, dynamic>>? slowNovel;
  Map<String, dynamic> feed(String category) => {
    'revision': 1,
    'total': 1,
    'complete': true,
    'has_more': false,
    'unavailable': 0,
    'job': null,
    'items': [
      {
        'id': category,
        'title': '$category work',
        'source': 'pixiv',
        'type': category,
        'author': 'Remote Author',
        'content_state': 'metadata',
      },
    ],
  };
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) async {
    if (path == '/pixiv/authors') {
      return {
        'items': [
          {'remote_id': '10', 'name': 'Remote Author'},
        ],
        'total': 1,
      };
    }
    if (path == '/pixiv/authors/10') {
      return {
        'remote_id': '10',
        'name': 'Remote Author',
        'description': 'Profile',
        'can_read_private': privateAllowed,
      };
    }
    if (path == '/pixiv/tasks/profile') {
      return {
        'status': profilePending ? 'queued' : 'completed',
        'author_id': '10',
      };
    }
    if (path == '/pixiv/tasks/feed') {
      if (feedPoll++ == 0) return {'status': 'queued'};
      empty = false;
      return {'status': 'completed'};
    }
    if (path.endsWith('/feed')) {
      filters.add({...?query});
      if (empty) {
        return {
          'revision': 0,
          'items': [],
          'total': 0,
          'job': null,
          'has_more': true,
        };
      }
      if (query?['category'] == 'novel' && slowNovel != null) {
        return slowNovel!.future;
      }
      return feed(query?['category'] ?? 'illustration');
    }
    throw const ApiFailure('不存在', 404);
  }

  @override
  Future<dynamic> post(String path, [Map<String, dynamic>? body]) async {
    posts.add({'path': path, ...?body});
    if (unavailable) throw const ApiFailure('未登录', 409);
    if (path.endsWith('/open')) {
      final novelBookmark = (body?['url_or_id'] as String).contains(
        '/bookmarks/novels',
      );
      return {
        'remote_id': '10',
        'section': novelBookmark ? 'bookmarks' : 'works',
        'category': novelBookmark ? 'novel' : 'illustration',
        'restrict': 'public',
        'job': {'id': 'profile', 'status': 'queued'},
      };
    }
    return {
      'job': {'id': 'feed', 'status': 'queued'},
    };
  }
}

Future<void> showAuthor(
  WidgetTester tester,
  AuthorApi api, {
  String target = '10',
  void Function(String)? onOpen,
}) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
    MaterialApp(
      home: AuthorScreen(
        api: api,
        target: target,
        onOpenItem: onOpen ?? (_) {},
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets(
    'author works and bookmarks each select illustration manga and novel',
    (tester) async {
      final api = AuthorApi();
      String? opened;
      await showAuthor(tester, api, onOpen: (id) => opened = id);
      expect(api.filters.last['section'], 'works');
      await tester.tap(find.text('小说'));
      await tester.pumpAndSettle();
      expect(api.filters.last['category'], 'novel');
      await tester.tap(find.text('收藏'));
      await tester.pumpAndSettle();
      expect(api.filters.last['section'], 'bookmarks');
      expect(api.filters.last['category'], 'novel');
      await tester.tap(find.text('漫画'));
      await tester.pumpAndSettle();
      expect(api.filters.last['category'], 'manga');
      await tester.tap(find.text('插画'));
      await tester.pumpAndSettle();
      expect(api.filters.last['category'], 'illustration');
      await tester.tap(find.text('illustration work'));
      await tester.pumpAndSettle();
      expect(opened, 'illustration');
      expect(
        api.posts.where(
          (request) => request['path'].toString().contains('/download'),
        ),
        isEmpty,
      );
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets('bookmark novel URLs select novel bookmarks immediately', (
    tester,
  ) async {
    final api = AuthorApi();
    await showAuthor(
      tester,
      api,
      target: 'https://www.pixiv.net/users/10/bookmarks/novels',
    );
    expect(api.filters.last['section'], 'bookmarks');
    expect(api.filters.last['category'], 'novel');
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('private bookmarks are offered only for confirmed own profile', (
    tester,
  ) async {
    final api = AuthorApi()..privateAllowed = true;
    await showAuthor(tester, api);
    await tester.tap(find.text('收藏'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('私密收藏'));
    await tester.pumpAndSettle();
    expect(api.filters.last['restrict'], 'private');
    await tester.tap(find.text('作者作品'));
    await tester.pumpAndSettle();
    expect(api.filters.last['restrict'], 'public');
    expect(find.text('私密收藏'), findsNothing);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('late novel response cannot overwrite newly chosen manga tab', (
    tester,
  ) async {
    final api = AuthorApi();
    await showAuthor(tester, api);
    api.slowNovel = Completer<Map<String, dynamic>>();
    await tester.tap(find.text('小说'));
    await tester.pump(); // A request in progress now displays a loading indicator.
    await tester.tap(find.text('漫画'));
    await tester.pumpAndSettle();
    expect(find.text('manga work'), findsOneWidget);
    api.slowNovel!.complete(api.feed('novel'));
    await tester.pumpAndSettle();
    expect(find.text('manga work'), findsOneWidget);
    expect(find.text('novel work'), findsNothing);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets(
    'queued profile shows acquisition pause guidance and no automatic bodies',
    (tester) async {
      final api = AuthorApi()..profilePending = true;
      await showAuthor(tester, api);
      expect(find.textContaining('采集暂停'), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'empty catalogue queues metadata then clears loading when job completes',
    (tester) async {
      final api = AuthorApi()..empty = true;
      await showAuthor(tester, api);
      await tester.pump(const Duration(seconds: 3));
      await tester.pumpAndSettle();
      expect(find.text('illustration work'), findsOneWidget);
      expect(find.byType(LinearProgressIndicator), findsNothing);
      expect(
        api.posts.any(
          (request) => request['path'].toString().endsWith('/feed'),
        ),
        true,
      );
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets('missing server Pixiv account gives actionable login message', (
    tester,
  ) async {
    final api = AuthorApi()..unavailable = true;
    await showAuthor(tester, api);
    expect(find.text('请先在网页「内容来源」登录 Pixiv 账号。'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
  });
}
