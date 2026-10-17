import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/comic_flow_reader.dart';
import 'package:qiye_library/models.dart';
import 'package:qiye_library/reader_screen.dart';

import 'comic_detail_test.dart' show ComicApi, pagePng;
import 'comic_flow_reader_test.dart' show MemoryPreferences, imagesSettle;
import 'reading_ui_test.dart' show book, phone;

Future<void> openReadComic(WidgetTester tester, ComicApi api) async {
  await phone(
    tester,
    ReaderScreen(
      api: api,
      item: LibraryItem.fromJson(book(id: 'comic', image: true, count: 8)),
      preferences: MemoryPreferences(true),
    ),
  );
  await imagesSettle(tester);
  // Read every page before testing save feedback. This isolates parent-layout
  // changes from first-visit image decoding and unknown page heights.
  final scroll = tester
      .widget<CustomScrollView>(find.byType(CustomScrollView))
      .controller!;
  for (var i = 0; i < 20; i++) {
    scroll.jumpTo(
      (scroll.offset + 300).clamp(0.0, scroll.position.maxScrollExtent),
    );
    await imagesSettle(tester);
  }
  expect(scroll.position.extentAfter, closeTo(0, .1));
  await tester.pump(const Duration(seconds: 1));
  await imagesSettle(tester);
  expect(find.textContaining('阅读进度未保存'), findsNothing);
}

void main() {
  late Uint8List portrait, landscape;
  setUpAll(() async {
    portrait = await pagePng(400, 600);
    landscape = await pagePng(600, 300);
  });

  testWidgets(
    'failed progress save does not move the comic viewport or visible page after bottom reversal',
    (tester) async {
      final api = ComicApi(portrait, landscape);
      await openReadComic(tester, api);
      api.failProgress = true;
      final scroll = tester
          .widget<CustomScrollView>(find.byType(CustomScrollView))
          .controller!;
      // Reverse a small distance from the bottom and let its debounced save fail.
      scroll.jumpTo(scroll.offset - 100);
      await imagesSettle(tester);
      final viewport = tester.getRect(find.byType(ComicFlowReader));
      final lastPage = tester.getRect(find.byKey(const ValueKey('comic-7')));
      await tester.pump(const Duration(seconds: 1));
      await imagesSettle(tester);
      expect(find.textContaining('阅读进度未保存'), findsOneWidget);
      expect(
        tester.getRect(find.byType(ComicFlowReader)),
        viewport,
        reason:
            'Network save feedback must not resize or displace reading content',
      );
      expect(
        tester.getRect(find.byKey(const ValueKey('comic-7'))).top,
        closeTo(lastPage.top, .1),
        reason: 'The same image point must stay under the reader\'s eye',
      );
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'successful save retry does not move the comic viewport or visible page',
    (tester) async {
      final api = ComicApi(portrait, landscape);
      await openReadComic(tester, api);
      api.failProgress = true;
      final scroll = tester
          .widget<CustomScrollView>(find.byType(CustomScrollView))
          .controller!;
      scroll.jumpTo(scroll.offset - 100);
      await imagesSettle(tester);
      await tester.pump(const Duration(seconds: 1));
      await imagesSettle(tester);
      expect(find.textContaining('阅读进度未保存'), findsOneWidget);
      final viewport = tester.getRect(find.byType(ComicFlowReader));
      final lastPage = tester.getRect(find.byKey(const ValueKey('comic-7')));
      api.failProgress = false;
      await tester.tap(find.text('重试'));
      await imagesSettle(tester);
      expect(find.textContaining('阅读进度未保存'), findsNothing);
      expect(
        tester.getRect(find.byType(ComicFlowReader)),
        viewport,
        reason:
            'A recovered connection must not resize or displace reading content',
      );
      expect(
        tester.getRect(find.byKey(const ValueKey('comic-7'))).top,
        closeTo(lastPage.top, .1),
      );
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );
}
