import 'package:flutter/material.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/article_body.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/models.dart';
import 'package:qiye_library/novel_reader_body.dart';
import 'package:qiye_library/reader_preferences.dart';
import 'package:qiye_library/reader_screen.dart';

import 'reading_ui_test.dart' show ReaderApi, book, phone;

class _SeriesFailureApi extends ReaderApi {
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) async {
    if (path == '/items/next') throw const ApiFailure('测试连接失败');
    return super.get(path, query: query);
  }
}

ScrollController textScroll(WidgetTester tester) => tester
    .widget<SingleChildScrollView>(find.byType(SingleChildScrollView).first)
    .controller!;

Finder paragraphWidgets() => find.byWidgetPredicate(
  (widget) =>
      widget is Positioned &&
      widget.key is ValueKey<String> &&
      (widget.key! as ValueKey<String>).value.startsWith('novel-paragraph-'),
);

void main() {
  setUp(() => FlutterSecureStorage.setMockInitialValues({}));

  testWidgets(
    'long chapter mounts only nearby paragraphs, restores exact ratio, '
    'and font cache round trip preserves position and preference',
    (tester) async {
      final api = ReaderApi()
        ..chapters = [
          {
            'index': 0,
            'title': '长章节',
            'text': List.generate(
              2400,
              (i) => '　　第$i段。这段正文用于验证长章节滚动和位置恢复。',
            ).join('\n'),
          },
        ];
      await phone(
        tester,
        ReaderScreen(api: api, item: LibraryItem.fromJson(book(offset: .63))),
      );
      var scroll = textScroll(tester);
      final originalExtent = scroll.position.maxScrollExtent;
      expect(scroll.offset / originalExtent, closeTo(.63, .00001));
      expect(paragraphWidgets().evaluate().length, lessThan(40));
      expect(find.textContaining('第0段。'), findsNothing);
      expect(api.saves, isEmpty);

      // Actual scrolling changes visible blocks, not the full chapter extent.
      await tester.drag(
        find.byType(SingleChildScrollView),
        const Offset(0, -450),
      );
      await tester.pumpAndSettle();
      final ratio = scroll.offset / scroll.position.maxScrollExtent;
      expect(ratio, greaterThan(.63));
      expect(scroll.position.maxScrollExtent, originalExtent);
      expect(paragraphWidgets().evaluate().length, lessThan(40));
      for (final label in ['增大字号', '减小字号']) {
        await tester.tap(find.byTooltip('文字大小'));
        await tester.pumpAndSettle();
        await tester.tap(find.text(label));
        await tester.pumpAndSettle();
        scroll = textScroll(tester);
        expect(
          scroll.offset / scroll.position.maxScrollExtent,
          closeTo(ratio, .00001),
        );
        expect(tester.takeException(), isNull);
      }
      expect(scroll.position.maxScrollExtent, originalExtent);
      expect(await const ReaderPreferences().fontSize(), 19);
      await tester.tap(find.byTooltip('文字大小'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('增大字号'));
      await tester.pumpAndSettle();
      expect(await const ReaderPreferences().fontSize(), 20);
      await tester.pumpWidget(const SizedBox());

      // A newly opened reader reads the persisted setting, not a State default.
      await phone(
        tester,
        ReaderScreen(api: ReaderApi(), item: LibraryItem.fromJson(book())),
      );
      expect(
        tester.widget<NovelReaderBody>(find.byType(NovelReaderBody)).fontSize,
        20,
      );
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets('chapter changes cancel old layout and identical chapters reset '
      'to the new section without writing the old offset', (tester) async {
    final same = List.generate(1200, (i) => '同一内容第$i段。验证切章。').join('\n');
    final api = ReaderApi()
      ..chapters = [
        {'index': 0, 'title': '相同标题', 'text': same},
        {'index': 1, 'title': '相同标题', 'text': same},
        {'index': 2, 'title': '短章', 'text': '结尾。'},
      ];
    await phone(
      tester,
      ReaderScreen(api: api, item: LibraryItem.fromJson(book(offset: .7))),
    );
    await tester.tap(find.byTooltip('下一节'));
    await tester.pumpAndSettle();
    expect(textScroll(tester).offset, 0);
    await tester.pump(const Duration(seconds: 1));
    expect(api.saves.last, containsPair('section', 1));
    expect(api.saves.last, containsPair('offset', 0.0));
    await tester.drag(
      find.byType(SingleChildScrollView),
      const Offset(0, -350),
    );
    await tester.pumpAndSettle();
    await tester.pump(const Duration(seconds: 1));
    expect(api.saves.last['offset'], greaterThan(0));

    // Start a font layout, then change chapter before it completes.
    await tester.tap(find.byTooltip('文字大小'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('增大字号'));
    await tester.pump();
    await tester.tap(find.byTooltip('下一节'));
    await tester.pumpAndSettle();
    await tester.pump(const Duration(seconds: 1));
    expect(find.text('结尾。'), findsOneWidget);
    expect(api.saves.last, containsPair('section', 2));
    expect(api.saves.last, containsPair('offset', 0.0));
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('measured extent matches eager text layout, including one huge '
      'paragraph; width and text scale changes preserve ratio', (tester) async {
    final controller = ScrollController();
    addTearDown(controller.dispose);
    // One very long paragraph exercises bounded soft-line chunks, then a
    // separate paragraph checks that only real paragraph ends add spacing.
    final text = '${List.filled(1600, '中文正文带标点。abc def ').join()}\n\n　　末段。';
    double width = 390, scale = 1;
    var ratio = .42;
    var restoring = false;
    controller.addListener(() {
      if (!restoring &&
          controller.hasClients &&
          controller.position.maxScrollExtent > 0) {
        ratio = controller.offset / controller.position.maxScrollExtent;
      }
    });
    Widget body(bool virtual) => MaterialApp(
      home: Scaffold(
        body: MediaQuery(
          data: MediaQueryData(textScaler: TextScaler.linear(scale)),
          child: Align(
            alignment: Alignment.topLeft,
            child: SizedBox(
              width: width,
              child: virtual
                  ? NovelReaderBody(
                      text: text,
                      title: '正文',
                      fontSize: 19,
                      restoreOffset: ratio,
                      controller: controller,
                      footer: const SizedBox(),
                      onLayoutStart: () => restoring = true,
                      onLayoutReady: () => restoring = false,
                    )
                  : SingleChildScrollView(
                      controller: controller,
                      padding: const EdgeInsets.fromLTRB(24, 12, 24, 36),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Builder(
                            builder: (context) => Text(
                              '正文',
                              style: Theme.of(
                                context,
                              ).textTheme.headlineSmall?.copyWith(height: 1.5),
                            ),
                          ),
                          const SizedBox(height: 22),
                          ArticleBody(text: text, fontSize: 19),
                          const SizedBox(height: 28 + 48),
                        ],
                      ),
                    ),
            ),
          ),
        ),
      ),
    );
    await tester.pumpWidget(body(true));
    await tester.pumpAndSettle();
    final original = controller.position.maxScrollExtent;
    expect(controller.offset / original, closeTo(.42, .00001));
    expect(paragraphWidgets().evaluate().length, lessThan(5));
    for (final config in [(600.0, 1.3), (390.0, 1.0)]) {
      width = config.$1;
      scale = config.$2;
      await tester.pumpWidget(body(true));
      await tester.pumpAndSettle();
      expect(
        controller.offset / controller.position.maxScrollExtent,
        closeTo(.42, .00001),
      );
      expect(tester.takeException(), isNull);
    }
    expect(controller.position.maxScrollExtent, original);
    restoring = true;
    await tester.pumpWidget(body(false));
    await tester.pumpAndSettle();
    expect(controller.position.maxScrollExtent, closeTo(original, .01));
    await tester.pumpWidget(const SizedBox());
  });

  test('invalid stored font preferences fall back safely', () async {
    FlutterSecureStorage.setMockInitialValues({'qiye.novel.font': 'NaN'});
    expect(await const ReaderPreferences().fontSize(), 19);
    FlutterSecureStorage.setMockInitialValues({'qiye.novel.font': '9999'});
    expect(await const ReaderPreferences().fontSize(), 30);
  });

  test(
    'large paragraph parsing works in an isolate and preserves indentation',
    () async {
      final text = List.filled(20000, '　　正文内容abcdef。\r\n').join();
      expect(text.length, greaterThan(256 * 1024));
      final parsed = await compute(parseArticleParagraphs, text);
      expect(parsed.length, 20000);
      expect(parsed.first, '　　正文内容abcdef。');
      expect(parsed.last, parsed.first);
    },
  );

  testWidgets(
    'series unavailable and fast failure both restore scroll tracking',
    (tester) async {
      for (final api in [ReaderApi(), _SeriesFailureApi()]) {
        api.chapters = [
          {
            'index': 0,
            'title': '正文',
            'text': List.filled(400, '当前作品正文。').join('\n'),
          },
        ];
        await phone(
          tester,
          ReaderScreen(
            api: api,
            item: LibraryItem.fromJson(book(series: true, offset: .3)),
          ),
        );
        await tester.tap(find.byTooltip('系列与上下篇'));
        await tester.pumpAndSettle();
        await tester.tap(find.text('下篇未下载'));
        await tester.pumpAndSettle();
        if (api is! _SeriesFailureApi) {
          await tester.tap(find.text('关闭'));
          await tester.pumpAndSettle();
        }
        api.saves.clear();
        await tester.drag(
          find.byType(SingleChildScrollView),
          const Offset(0, -350),
        );
        await tester.pumpAndSettle();
        await tester.pump(const Duration(seconds: 1));
        expect(api.saves.last, containsPair('path', '/items/novel/progress'));
        expect(api.saves.last['offset'], greaterThan(.3));
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox());
      }
    },
  );
}
