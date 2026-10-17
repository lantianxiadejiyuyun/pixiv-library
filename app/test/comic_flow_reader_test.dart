import 'dart:async';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/comic_flow_reader.dart';
import 'package:qiye_library/api_image.dart';
import 'package:qiye_library/models.dart';
import 'package:qiye_library/reader_preferences.dart';
import 'package:qiye_library/reader_screen.dart';
import 'comic_detail_test.dart' show ComicApi, pagePng;
import 'reading_ui_test.dart' show book, phone;

class MemoryPreferences extends ReaderPreferences {
  bool enabled;
  final saves = <bool>[];
  MemoryPreferences(this.enabled);
  @override
  Future<bool> flowMode() async => enabled;
  @override
  Future<void> saveFlowMode(bool flow) async {
    enabled = flow;
    saves.add(flow);
  }
}

class DelayedReloadComicApi extends ComicApi {
  Completer<Uint8List>? reload;
  DelayedReloadComicApi(super.portrait, super.landscape);
  @override
  Future<Uint8List> image(String path) {
    if (path == '/assets/comic-1/content' && reload != null) {
      imageRequests.add(path);
      return reload!.future;
    }
    return super.image(path);
  }
}

class AlternatingComicApi extends ComicApi {
  bool delayRevisits = false;
  AlternatingComicApi(super.portrait, super.landscape);
  @override
  Future<Uint8List> image(String path) async {
    final revisit = imageRequests.contains(path);
    imageRequests.add(path);
    if (delayRevisits && revisit) {
      await Future<void>.delayed(const Duration(milliseconds: 120));
    }
    final page = int.parse(
      RegExp(r'comic-(\d+)/content').firstMatch(path)![1]!,
    );
    return page.isEven ? portrait : landscape;
  }
}

Future<void> imagesSettle(WidgetTester tester) async {
  await tester.pump();
  await tester.runAsync(() async {
    await Future<void>.delayed(const Duration(milliseconds: 80));
  });
  await tester.pumpAndSettle();
}

void main() {
  late Uint8List portrait, landscape;
  setUpAll(() async {
    portrait = await pagePng(400, 600);
    landscape = await pagePng(600, 300);
  });

  testWidgets(
    'returning from the bottom keeps the extent of visited mixed pages stable',
    (tester) async {
      final api = AlternatingComicApi(portrait, landscape);
      await phone(
        tester,
        Scaffold(
          body: ComicFlowReader(
            api: api,
            itemId: 'comic',
            assets: LibraryItem.fromJson(
              book(id: 'comic', image: true, count: 12),
            ).assets,
            initialPage: 0,
            controller: ComicFlowController(),
            onPosition: (_, _) {},
          ),
        ),
      );
      await imagesSettle(tester);
      final scroll = tester
          .widget<CustomScrollView>(find.byType(CustomScrollView))
          .controller!;
      for (var i = 0; i < 20; i++) {
        final next = (scroll.offset + 300).clamp(
          0.0,
          scroll.position.maxScrollExtent,
        );
        scroll.jumpTo(next);
        await imagesSettle(tester);
      }
      expect(api.imageRequests.toSet().length, 12);
      expect(scroll.position.extentAfter, closeTo(0, .1));
      final end = scroll.position.maxScrollExtent;
      for (var i = 0; i < 10; i++) {
        final target = scroll.offset - 120;
        scroll.jumpTo(target);
        await imagesSettle(tester);
        expect(
          scroll.position.maxScrollExtent,
          closeTo(end, .1),
          reason: 'Returning step $i must not change the bottom',
        );
        expect(scroll.offset, closeTo(target, .1));
      }
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  for (final platform in [TargetPlatform.android, TargetPlatform.iOS]) {
    testWidgets(
      'repeated bottom reversal with delayed recycled images on $platform',
      (tester) async {
        final api = AlternatingComicApi(portrait, landscape);
        var current = 6;
        await phone(
          tester,
          Theme(
            data: ThemeData(platform: platform),
            child: Scaffold(
              body: ComicFlowReader(
                api: api,
                itemId: 'comic',
                assets: LibraryItem.fromJson(
                  book(id: 'comic', image: true, count: 18),
                ).assets,
                initialPage: 6,
                controller: ComicFlowController(),
                onPosition: (page, _) => current = page,
              ),
            ),
          ),
        );
        await imagesSettle(tester);
        final view = find.byType(CustomScrollView);
        final scroll = tester.widget<CustomScrollView>(view).controller!;
        for (var i = 0; i < 30; i++) {
          scroll.jumpTo(
            (scroll.offset + 250).clamp(0.0, scroll.position.maxScrollExtent),
          );
          await imagesSettle(tester);
        }
        expect(api.imageRequests, contains('/assets/comic-17/content'));
        expect(scroll.position.extentAfter, closeTo(0, .1));
        final end = scroll.position.maxScrollExtent;
        api.delayRevisits = true;
        for (var round = 0; round < 3; round++) {
          // Overscroll at the bottom, then reverse the gesture while images that
          // were disposed below/above the cache boundary are being recreated.
          await tester.drag(view, const Offset(0, -180));
          await imagesSettle(tester);
          for (var step = 0; step < 3; step++) {
            final previous = scroll.offset;
            await tester.drag(view, const Offset(0, 180));
            await imagesSettle(tester);
            expect(scroll.offset, lessThan(previous));
            expect(scroll.position.maxScrollExtent, closeTo(end, .1));
          }
          expect(current, lessThan(16));
          scroll.jumpTo(end);
          await imagesSettle(tester);
          expect(scroll.position.extentAfter, closeTo(0, .1));
        }
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox());
      },
    );
  }

  testWidgets(
    'recycled landscape page keeps its height while cached bytes reload',
    (tester) async {
      final api = DelayedReloadComicApi(portrait, landscape);
      final controller = ComicFlowController();
      await phone(
        tester,
        Scaffold(
          body: ComicFlowReader(
            api: api,
            itemId: 'comic',
            assets: LibraryItem.fromJson(
              book(id: 'comic', image: true, count: 40),
            ).assets,
            initialPage: 0,
            controller: controller,
            onPosition: (_, _) {},
          ),
        ),
      );
      await imagesSettle(tester);
      final page = find.byKey(const ValueKey('comic-1'));
      expect(tester.getSize(page).height, closeTo(195, .01));
      final scroll = tester
          .widget<CustomScrollView>(find.byType(CustomScrollView))
          .controller!;
      scroll.jumpTo(5000);
      await imagesSettle(tester);
      expect(page, findsNothing);
      api.reload = Completer<Uint8List>();
      scroll.jumpTo(560);
      await tester.pump();
      expect(tester.getSize(page).height, closeTo(195, .01));
      final offset = scroll.offset;
      api.reload!.complete(landscape);
      await imagesSettle(tester);
      expect(tester.getSize(page).height, closeTo(195, .01));
      expect(scroll.offset, closeTo(offset, .01));
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'failed flow image retries independently without leaving reader',
    (tester) async {
      final api = ComicApi(portrait, landscape)
        ..failures.add('/assets/comic-0/content');
      await phone(
        tester,
        ReaderScreen(
          api: api,
          item: LibraryItem.fromJson(book(id: 'comic', image: true, count: 8)),
          preferences: MemoryPreferences(true),
        ),
      );
      expect(find.byTooltip('重新加载图片'), findsOneWidget);
      await tester.tap(find.byTooltip('重新加载图片'));
      // The reader's double-tap gesture waits before accepting a single tap.
      await tester.pump(const Duration(milliseconds: 350));
      await imagesSettle(tester);
      expect(find.byTooltip('重新加载图片'), findsNothing);
      expect(
        api.imageRequests
            .where((path) => path == '/assets/comic-0/content')
            .length,
        2,
      );
      expect(find.byType(ComicFlowReader), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'reader switches between horizontal and continuous vertical mode at current page',
    (tester) async {
      final api = ComicApi(portrait, landscape);
      final prefs = MemoryPreferences(false);
      await phone(
        tester,
        ReaderScreen(
          api: api,
          item: LibraryItem.fromJson(book(id: 'comic', image: true, count: 40)),
          preferences: prefs,
        ),
      );
      expect(find.byType(PageView), findsOneWidget);
      await tester.tap(find.byTooltip('下一 P'));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('阅读方式'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('竖向瀑布流'));
      await imagesSettle(tester);
      expect(find.byType(ComicFlowReader), findsOneWidget);
      expect(find.byType(PageView), findsNothing);
      expect(find.text('第 2 / 40 P'), findsOneWidget);
      final page = find.byWidgetPredicate(
        (w) => w is ApiImage && w.path == '/assets/comic-1/content',
      );
      final image = find.descendant(of: page, matching: find.byType(Image));
      expect(tester.getSize(image), const Size(390, 195));
      expect(prefs.saves, [true]);
      await tester.tap(find.byTooltip('阅读方式'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('横向翻页'));
      await tester.pumpAndSettle();
      expect(find.byType(PageView), findsOneWidget);
      expect(find.text('第 2 / 40 P'), findsOneWidget);
      expect(prefs.saves, [true, false]);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'restores deep page and fractional position without loading all preceding pages; scroll saves actual page',
    (tester) async {
      final api = ComicApi(portrait, landscape);
      final data = book(id: 'comic', image: true, count: 1000)
        ..['progress'] = {'page': 900, 'offset': .2};
      await phone(
        tester,
        ReaderScreen(
          api: api,
          item: LibraryItem.fromJson(data),
          preferences: MemoryPreferences(true),
        ),
      );
      await imagesSettle(tester);
      expect(find.text('第 901 / 1000 P'), findsOneWidget);
      expect(api.imageRequests.length, lessThan(10));
      expect(api.imageRequests, contains('/assets/comic-900/content'));
      expect(api.imageRequests, isNot(contains('/assets/comic-0/content')));
      await tester.pump(const Duration(seconds: 1));
      await tester.pumpAndSettle();
      expect(api.saves.last['page'], 900);
      expect(api.saves.last['offset'] as double, closeTo(.2, .02));
      final scroll = tester
          .widget<CustomScrollView>(find.byType(CustomScrollView))
          .controller!;
      scroll.jumpTo(650);
      await imagesSettle(tester);
      expect(find.text('第 902 / 1000 P'), findsOneWidget);
      await tester.pump(const Duration(seconds: 1));
      await tester.pumpAndSettle();
      expect(api.saves.last['page'], 901);
      // Negative scroll extents reach preceding images in their correct order.
      scroll.jumpTo(-150);
      await imagesSettle(tester);
      expect(find.text('第 900 / 1000 P'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'flow supports scrolling and double tap zoom without obscuring images or losing position',
    (tester) async {
      final api = ComicApi(portrait, landscape);
      await phone(
        tester,
        ReaderScreen(
          api: api,
          item: LibraryItem.fromJson(book(id: 'comic', image: true, count: 8)),
          preferences: MemoryPreferences(true),
        ),
      );
      await imagesSettle(tester);
      await tester.tap(find.byTooltip('页面目录'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('第 5 P'));
      await imagesSettle(tester);
      expect(find.text('第 5 / 8 P'), findsOneWidget);
      expect(find.byIcon(Icons.zoom_in), findsNothing);
      final image = find.byKey(const ValueKey('comic-4'));
      final scroll = tester
          .widget<CustomScrollView>(find.byType(CustomScrollView))
          .controller!;
      await tester.drag(image, const Offset(0, -120));
      await imagesSettle(tester);
      expect(scroll.offset, greaterThan(0));
      final offset = scroll.offset;
      await tester.pump(const Duration(seconds: 1));
      await tester.pumpAndSettle();
      final saved = Map<String, dynamic>.from(api.saves.last);
      expect(saved['page'], 4);
      expect(saved['offset'] as double, greaterThan(0));
      await tester.tap(image);
      await tester.pumpAndSettle(const Duration(milliseconds: 400));
      expect(find.byType(Dialog), findsNothing);
      await tester.tap(image);
      await tester.pump(const Duration(milliseconds: 100));
      await tester.tap(image);
      await tester.pumpAndSettle();
      expect(find.byType(Dialog), findsOneWidget);
      expect(find.byType(InteractiveViewer), findsOneWidget);
      await tester.tap(find.byTooltip('关闭放大'));
      await tester.pumpAndSettle();
      expect(find.text('第 5 / 8 P'), findsOneWidget);
      expect(scroll.offset, closeTo(offset, .01));
      expect(api.saves.last, saved);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets('novels and mixed assets keep their existing reading controls', (
    tester,
  ) async {
    final api = ComicApi(portrait, landscape);
    await phone(
      tester,
      ReaderScreen(
        api: api,
        item: LibraryItem.fromJson(book()),
        preferences: MemoryPreferences(true),
      ),
    );
    expect(find.byType(ComicFlowReader), findsNothing);
    expect(find.byTooltip('阅读方式'), findsNothing);
    expect(find.text('这是一段短正文。'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
  });
  testWidgets(
    'flow controller remains attached when a series replaces its reader',
    (tester) async {
      final api = ComicApi(portrait, landscape),
          controller = ComicFlowController();
      final assets = LibraryItem.fromJson(
        book(id: 'comic', image: true, count: 10),
      ).assets;
      int current = -1;
      Widget screen(String id) => Scaffold(
        body: ComicFlowReader(
          key: ValueKey(id),
          api: api,
          itemId: id,
          assets: assets,
          initialPage: 2,
          controller: controller,
          onPosition: (page, _) => current = page,
        ),
      );
      await phone(tester, screen('first'));
      await imagesSettle(tester);
      await phone(tester, screen('next'));
      await imagesSettle(tester);
      controller.jumpTo(6);
      await imagesSettle(tester);
      expect(current, 6);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );
}
