import 'dart:async';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/comic_flow_reader.dart';
import 'package:qiye_library/models.dart';
import 'comic_detail_test.dart' show ComicApi, pagePng;
import 'comic_flow_reader_test.dart' show imagesSettle;
import 'reading_ui_test.dart' show book;

class LatePageApi extends ComicApi {
  final pending = <int, Completer<Uint8List>>{};
  LatePageApi(super.portrait, super.landscape);
  @override
  Future<Uint8List> image(String path) {
    imageRequests.add(path);
    final index = int.parse(
      RegExp(r'comic-(\d+)/content').firstMatch(path)![1]!,
    );
    return pending[index]?.future ?? Future.value(Uint8List.fromList(portrait));
  }
}

Future<void> settleDecoded(WidgetTester tester) async {
  // Separate bytes completion from the subsequent asynchronous codec frame.
  for (var phase = 0; phase < 2; phase++) {
    await tester.pump();
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 50)),
    );
  }
  await tester.pump();
  await tester.pump();
}

Future<ScrollController> openFlow(
  WidgetTester tester,
  LatePageApi api, {
  int initial = 0,
  double offset = 0,
  int count = 8,
}) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: ComicFlowReader(
          api: api,
          itemId: 'comic',
          assets: LibraryItem.fromJson(
            book(id: 'comic', image: true, count: count),
          ).assets,
          initialPage: initial,
          initialOffset: offset,
          controller: ComicFlowController(),
          onPosition: (_, _) {},
        ),
      ),
    ),
  );
  await settleDecoded(tester);
  return tester
      .widget<CustomScrollView>(find.byType(CustomScrollView))
      .controller!;
}

void main() {
  late Uint8List portrait, landscape, tall;
  setUpAll(() async {
    portrait = await pagePng(400, 600);
    landscape = await pagePng(600, 300);
    tall = await pagePng(400, 1000);
  });
  for (final initial in [0, 7]) {
    testWidgets(
      'late first image preserves visible page and active reverse drag from anchor $initial',
      (tester) async {
        final api = LatePageApi(portrait, landscape)
          ..pending[6] = Completer<Uint8List>();
        tester.view.reset();
        tester.view.physicalSize = const Size(390, 844);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: ComicFlowReader(
                api: api,
                itemId: 'comic',
                assets: LibraryItem.fromJson(
                  book(id: 'comic', image: true, count: 8),
                ).assets,
                initialPage: initial,
                controller: ComicFlowController(),
                onPosition: (_, _) {},
              ),
            ),
          ),
        );
        await settleDecoded(tester);
        final view = find.byType(CustomScrollView);
        final scroll = tester.widget<CustomScrollView>(view).controller!;
        for (var i = 0; i < 20; i++) {
          scroll.jumpTo(
            (scroll.offset + 250).clamp(
              scroll.position.minScrollExtent,
              scroll.position.maxScrollExtent,
            ),
          );
          await settleDecoded(tester);
        }
        expect(api.imageRequests, contains('/assets/comic-7/content'));
        // When reopening at the last page, first move into the preceding page.
        if (initial == 7) {
          scroll.jumpTo(-300);
          await settleDecoded(tester);
        }
        final gesture = await tester.startGesture(const Offset(200, 500));
        await gesture.moveBy(const Offset(0, 30));
        await tester.pump();
        await gesture.moveBy(const Offset(0, 100));
        await tester.pump();
        final last = find.byKey(const ValueKey('comic-7'));
        final before = tester.getTopLeft(last).dy;
        api.pending[6]!.complete(Uint8List.fromList(landscape));
        await settleDecoded(tester);
        expect(
          tester.getSize(find.byKey(const ValueKey('comic-6'))).height,
          closeTo(195, .5),
        );
        expect(
          tester.getTopLeft(last).dy,
          closeTo(before, .5),
          reason:
              'An already visible image must stay under the finger when an earlier image first loads',
        );
        await gesture.moveBy(const Offset(0, 40));
        await tester.pump();
        expect(
          tester.getTopLeft(last).dy,
          closeTo(before + 40, .5),
          reason: 'Height correction must not cancel the active drag',
        );
        await gesture.up();
        await imagesSettle(tester);
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox());
      },
    );
  }

  testWidgets(
    'opposite size changes preserve a loaded page even when total height is unchanged',
    (tester) async {
      final api = LatePageApi(portrait, landscape)
        ..pending[1] = Completer<Uint8List>()
        ..pending[3] = Completer<Uint8List>();
      final scroll = await openFlow(tester, api, count: 4);
      scroll.jumpTo(1000);
      await settleDecoded(tester);
      final page = find.byKey(const ValueKey('comic-2'));
      final before = tester.getTopLeft(page).dy;
      final end = scroll.position.maxScrollExtent;
      api.pending[1]!.complete(Uint8List.fromList(landscape));
      api.pending[3]!.complete(Uint8List.fromList(tall));
      await settleDecoded(tester);
      expect(
        tester.getSize(find.byKey(const ValueKey('comic-1'))).height,
        closeTo(195, .5),
      );
      expect(
        tester.getSize(find.byKey(const ValueKey('comic-3'))).height,
        closeTo(975, .5),
      );
      expect(tester.getTopLeft(page).dy, closeTo(before, .5));
      expect(scroll.position.maxScrollExtent, closeTo(end, .5));
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'late page behind a visible page in the negative sliver preserves its screen position',
    (tester) async {
      final api = LatePageApi(portrait, landscape)
        ..pending[6] = Completer<Uint8List>();
      final scroll = await openFlow(tester, api, initial: 7);
      scroll.jumpTo(-900);
      await settleDecoded(tester);
      final page = find.byKey(const ValueKey('comic-5'));
      final before = tester.getTopLeft(page).dy;
      api.pending[6]!.complete(Uint8List.fromList(landscape));
      await settleDecoded(tester);
      expect(tester.getTopLeft(page).dy, closeTo(before, .5));
      expect(scroll.offset, lessThan(0));
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'late short final page does not snap the preceding image during an active drag',
    (tester) async {
      final api = LatePageApi(portrait, landscape)
        ..pending[7] = Completer<Uint8List>();
      final scroll = await openFlow(tester, api);
      scroll.jumpTo(scroll.position.maxScrollExtent - 100);
      await settleDecoded(tester);
      final gesture = await tester.startGesture(const Offset(200, 200));
      await gesture.moveBy(const Offset(0, 30));
      await tester.pump();
      await gesture.moveBy(const Offset(0, 30));
      await tester.pump();
      final page = find.byKey(const ValueKey('comic-6'));
      final before = tester.getTopLeft(page).dy;
      api.pending[7]!.complete(Uint8List.fromList(landscape));
      await settleDecoded(tester);
      expect(tester.getTopLeft(page).dy, closeTo(before, .5));
      await gesture.moveBy(const Offset(0, 40));
      await tester.pump();
      expect(tester.getTopLeft(page).dy, closeTo(before + 40, .5));
      await gesture.up();
      await imagesSettle(tester);
      expect(scroll.position.outOfRange, isFalse);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets(
    'restoring a fraction of a short final page clamps without a startup bounce',
    (tester) async {
      final api = LatePageApi(portrait, landscape)
        ..pending[7] = Completer<Uint8List>();
      final scroll = await openFlow(tester, api, initial: 7, offset: .8);
      api.pending[7]!.complete(Uint8List.fromList(landscape));
      await settleDecoded(tester);
      expect(
        tester.getSize(find.byKey(const ValueKey('comic-7'))).height,
        closeTo(195, .5),
      );
      expect(scroll.offset, 0);
      expect(scroll.position.isScrollingNotifier.value, isFalse);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );

  testWidgets('changing the reader width preserves the visible page fraction', (
    tester,
  ) async {
    final api = LatePageApi(portrait, landscape);
    final scroll = await openFlow(
      tester,
      api,
      initial: 20,
      offset: .3,
      count: 50,
    );
    await settleDecoded(tester);
    final page = find.byKey(const ValueKey('comic-20'));
    double fraction() =>
        -tester.getTopLeft(page).dy / (tester.getSize(page).height + 4);
    expect(fraction(), closeTo(.3, .005));
    tester.view.physicalSize = const Size(700, 844);
    await settleDecoded(tester);
    expect(fraction(), closeTo(.3, .005));
    expect(scroll.position.outOfRange, isFalse);
    expect(api.imageRequests.toSet().length, lessThan(10));
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets(
    'late image geometry and a width change in the same frame preserve the visible page fraction',
    (tester) async {
      final api = LatePageApi(portrait, landscape)
        ..pending[1] = Completer<Uint8List>();
      final scroll = await openFlow(tester, api);
      scroll.jumpTo(1000);
      await settleDecoded(tester);
      final page = find.byKey(const ValueKey('comic-2'));
      final delayed = find.byKey(const ValueKey('comic-1'));
      double fraction() =>
          -tester.getTopLeft(page).dy / (tester.getSize(page).height + 4);
      final before = fraction();
      expect(before, lessThan(0));

      api.pending[1]!.complete(Uint8List.fromList(landscape));
      await tester.pump();
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 80)),
      );
      // Decode reports the new ratio after this frame. The row still occupies
      // its old placeholder height until the queued geometry update is laid out.
      await tester.pump();
      expect(tester.getSize(delayed).height, closeTo(585, .5));
      tester.view.physicalSize = const Size(700, 844);
      await settleDecoded(tester);

      expect(tester.getSize(delayed).height, closeTo(350, .5));
      expect(
        fraction(),
        closeTo(before, .005),
        reason: 'Resize must include the image correction queued before layout',
      );
      expect(scroll.position.outOfRange, isFalse);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );
}
