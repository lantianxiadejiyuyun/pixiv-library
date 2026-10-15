import 'dart:typed_data';
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/api_image.dart';
import 'package:qiye_library/library_screen.dart';
import 'package:qiye_library/reader_screen.dart';
import 'reading_ui_test.dart' show ReaderApi, book, phone;

class ComicApi extends ReaderApi {
  final imageRequests = <String>[];
  final failures = <String>{};
  final Uint8List portrait, landscape;
  ComicApi(this.portrait, this.landscape) {
    listed = book(id: 'comic', image: true, count: 40);
    // The detail must use stored page positions, not response array order.
    listed['assets'] = (listed['assets'] as List).reversed.toList();
    listed['progress'] = {'page': 17};
  }
  @override
  Future<Uint8List> image(String path) async {
    imageRequests.add(path);
    if (failures.remove(path)) throw const ApiFailure('图片暂时无法读取');
    return path.endsWith('comic-1/content') ? landscape : portrait;
  }
}

Future<Uint8List> pagePng(int width, int height) async {
  final recorder = ui.PictureRecorder();
  final canvas = Canvas(recorder);
  canvas.drawColor(const Color(0xffc9d4ee), BlendMode.src);
  final picture = recorder.endRecording();
  final image = await picture.toImage(width, height);
  final data = await image.toByteData(format: ui.ImageByteFormat.png);
  image.dispose();
  picture.dispose();
  return data!.buffer.asUint8List();
}

Future<void> openComic(WidgetTester tester, ComicApi api) async {
  await phone(tester, LibraryScreen(api: api, onSettings: () {}));
  await tester.tap(find.text('作品 comic'));
  await tester.pumpAndSettle();
}

void main() {
  late Uint8List portrait, landscape;
  setUpAll(() async {
    portrait = await pagePng(400, 600);
    landscape = await pagePng(600, 300);
  });
  testWidgets(
    'detail only loads cover and metadata; continuous images belong to reader',
    (tester) async {
      final api = ComicApi(portrait, landscape);
      await openComic(tester, api);
      await tester.drag(find.byType(ListView).last, const Offset(0, -1500));
      await tester.pumpAndSettle();
      expect(api.imageRequests, isEmpty);
      expect(api.saves, isEmpty);
      expect(
        find.byWidgetPredicate((w) => w is ApiImage && w.naturalHeight),
        findsNothing,
      );
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );
  testWidgets(
    'detail reading entry restores saved original asset index without refreshing on return',
    (tester) async {
      final api = ComicApi(portrait, landscape);
      await openComic(tester, api);
      final requests = api.gets.length;
      await tester.tap(find.text('开始 / 继续阅读'));
      await tester.pumpAndSettle();
      expect(find.text('第 18 / 40 P'), findsOneWidget);
      await tester.pump(const Duration(seconds: 1));
      await tester.pumpAndSettle();
      expect(api.saves.last['page'], 17);
      await tester.tap(find.byTooltip('返回书库'));
      await tester.pumpAndSettle();
      expect(find.byType(ReaderScreen), findsNothing);
      expect(api.gets.length, requests);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    },
  );
  for (final novel in [false, true]) {
    testWidgets(
      '${novel ? "novel" : "undownloaded comic"} detail does not fetch comic images',
      (tester) async {
        final api = ComicApi(portrait, landscape)
          ..listed = book(
            id: 'comic',
            image: !novel,
            state: novel ? 'ready' : 'metadata',
          );
        await openComic(tester, api);
        expect(api.imageRequests, isEmpty);
        expect(
          find.byWidgetPredicate((w) => w is ApiImage && w.naturalHeight),
          findsNothing,
        );
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox());
      },
    );
  }
}
