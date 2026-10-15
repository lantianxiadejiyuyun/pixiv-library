// Run explicitly: flutter test integration/preview_test.dart
// These are native widget renders with synthetic fixtures, not device screenshots.
import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/binding_screen.dart';
import 'package:qiye_library/library_screen.dart';
import 'package:qiye_library/models.dart';
import 'package:qiye_library/reader_screen.dart';
import 'package:qiye_library/tag_browser_screen.dart';
import 'package:qiye_library/author_screen.dart';
import '../test/author_ui_test.dart' show AuthorApi;
import '../test/reading_ui_test.dart' show ReaderApi, book;
import '../test/session_api_test.dart' show MemoryStore;

class _PreviewApi extends ReaderApi {
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) {
    if (path == '/tags') {
      return Future.value([
        {'id': 'tag-blue', 'name': 'blue_eyes', 'name_zh': '蓝眼睛', 'count': 12},
        {'id': 'tag-library', 'name': 'library', 'name_zh': '图书馆', 'count': 4},
        {'id': 'tag-daily', 'name': '日常', 'count': 8},
      ]);
    }
    if (path == '/items') {
      return Future.value({
        'total': 4,
        'items': [
          {
            ...book(),
            'title': '海边来信 · 上篇（示例）',
            'author': '合成示例',
            'source': 'pixiv',
          },
          {
            ...book(id: 'second', state: 'queued'),
            'title': '海边来信 · 下篇（示例）',
            'author': '合成示例',
            'source': 'pixiv',
          },
          {
            ...book(id: 'comic', image: true, count: 3),
            'title': '雨后街角（示例）',
            'author': '合成示例',
            'source': 'local',
          },
          {
            ...book(id: 'waiting', state: 'metadata'),
            'title': '待下载作品（示例）',
            'author': '合成示例',
            'source': 'local',
          },
        ],
      });
    }
    return super.get(path, query: query);
  }
}

class _AuthorPreviewApi extends AuthorApi {
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) {
    if (path == '/pixiv/authors/10') {
      return Future.value({
        'remote_id': '10',
        'name': '合成示例作者',
        'description': '作者资料示例，仅用于界面预览。',
        'can_read_private': true,
      });
    }
    return super.get(path, query: query);
  }

  @override
  Map<String, dynamic> feed(String category) => {
    ...super.feed(category),
    'items': [
      {
        'id': 'preview-author-work',
        'title': 'Letters from the sea',
        'title_zh': '海边来信（合成示例）',
        'source': 'pixiv',
        'type': category,
        'author': '合成示例作者',
        'content_state': 'metadata',
      },
    ],
  };
}

void main() {
  testWidgets(
    'render native binding, library and novel previews at phone width',
    (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      // Use a local CJK font for deterministic, legible headless previews.
      final fontPath =
          Platform.environment['QIYE_PREVIEW_FONT'] ??
          'C:/Windows/Fonts/msyh.ttc';
      await tester.runAsync(() async {
        final bytes = await File(fontPath).readAsBytes();
        final loader = FontLoader('PreviewSans')
          ..addFont(Future.value(ByteData.sublistView(bytes)));
        await loader.load();
        final icons = FontLoader('MaterialIcons')
          ..addFont(rootBundle.load('fonts/MaterialIcons-Regular.otf'));
        await icons.load();
      });
      final boundary = GlobalKey();
      Future<void> capture(
        Widget screen,
        String filename, {
        Future<void> Function()? prepare,
      }) async {
        await tester.pumpWidget(
          RepaintBoundary(
            key: boundary,
            child: MaterialApp(
              debugShowCheckedModeBanner: false,
              locale: const Locale('zh', 'CN'),
              supportedLocales: const [Locale('zh', 'CN'), Locale('en')],
              localizationsDelegates: GlobalMaterialLocalizations.delegates,
              theme: ThemeData(
                useMaterial3: true,
                fontFamily: 'PreviewSans',
                colorScheme: ColorScheme.fromSeed(
                  seedColor: const Color(0xff5967aa),
                ),
                appBarTheme: const AppBarTheme(centerTitle: false),
              ),
              home: screen,
            ),
          ),
        );
        await tester.pumpAndSettle();
        if (prepare != null) {
          await prepare();
          await tester.pumpAndSettle();
        }
        expect(tester.takeException(), isNull);
        await tester.runAsync(() async {
          final object =
              boundary.currentContext!.findRenderObject()!
                  as RenderRepaintBoundary;
          final image = await object.toImage(pixelRatio: 2);
          final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
          final file = File('../docs/$filename');
          await file.parent.create(recursive: true);
          await file.writeAsBytes(bytes!.buffer.asUint8List());
          image.dispose();
        });
      }

      await capture(
        BindingScreen(pairing: PairingService(MemoryStore()), onBound: (_) {}),
        'app-binding-preview.png',
      );
      final api = _PreviewApi()
        ..chapters = [
          {
            'index': 0,
            'title': '第一章 · 海边来信',
            'text':
                '清晨的风吹过窗边，桌上的信封轻轻翻动。\n\n这是一段用于界面验收的原创合成文字，与书库中的真实作品无关。\n\n她沿着海岸向前走，听见远处灯塔传来的钟声。每到一处熟悉的转角，信中的句子就变得更清晰了一些。\n\n午后，天空渐渐放晴。她把信重新折好，放回口袋，准备走向下一段旅程。',
          },
          {'index': 1, 'title': '第二章 · 灯塔', 'text': '这是第二节的合成示例正文。'},
          {'index': 2, 'title': '第三章 · 归途', 'text': '这是第三节的合成示例正文。'},
        ];
      await capture(
        LibraryScreen(api: api, onSettings: () {}),
        'app-library-preview.png',
      );
      await capture(
        LibraryScreen(api: api, onSettings: () {}),
        'app-drawer-preview.png',
        prepare: () async {
          await tester.tap(find.byTooltip('打开侧边栏'));
        },
      );
      await capture(
        TagBrowserScreen(api: api, source: 'pixiv'),
        'app-tag-search-preview.png',
      );
      final authorApi = _AuthorPreviewApi();
      await capture(
        AuthorScreen(
          api: authorApi,
          target: '10',
          initialSection: 'bookmarks',
          onOpenItem: (_) {},
        ),
        'app-author-bookmarks-preview.png',
        prepare: () async {
          await tester.tap(find.text('小说'));
        },
      );
      authorApi.close();
      await capture(
        ReaderScreen(
          api: api,
          item: LibraryItem.fromJson({
            ...book(series: true),
            'title': '海边来信（合成示例）',
          }),
        ),
        'app-novel-preview.png',
      );
      await tester.pumpWidget(const SizedBox());
      api.close();
    },
  );
}
