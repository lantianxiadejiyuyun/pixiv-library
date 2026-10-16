import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/app_image_cache.dart';
import 'package:qiye_library/image_cache_card.dart';

class CacheSettingsApi extends LibraryApi {
  CacheSettingsApi() : super(server: Uri.parse('https://library.test'));
  int bytes = 2 * 1024 * 1024, entries = 3, clears = 0;
  bool failClear = false;
  Completer<void>? pendingClear;

  @override
  Future<ImageCacheStats> imageCacheStats() async => ImageCacheStats(
    bytes: bytes,
    entries: entries,
    maxBytes: 16 * 1024 * 1024 * 1024,
    memoryBytes: 0,
  );

  @override
  Future<void> clearImageCache() async {
    clears++;
    if (failClear) throw Exception('private path must not appear');
    if (pendingClear != null) await pendingClear!.future;
    bytes = 0;
    entries = 0;
  }
}

void main() {
  testWidgets('cache settings show usage and avoid duplicate clear requests', (
    tester,
  ) async {
    final api = CacheSettingsApi()..pendingClear = Completer<void>();
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: ImageCacheCard(api: api)),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('2.0 MB · 3 张图片'), findsOneWidget);
    expect(find.text('上限 16.0 GB，较早使用的图片自动清理。'), findsOneWidget);
    await tester.tap(find.text('清除图片缓存'));
    await tester.pump();
    expect(api.clears, 1);
    expect(
      tester.widget<OutlinedButton>(find.byType(OutlinedButton)).onPressed,
      isNull,
    );
    api.pendingClear!.complete();
    await tester.pumpAndSettle();
    expect(find.text('0.0 MB · 0 张图片'), findsOneWidget);
    expect(find.text('图片缓存已清除'), findsOneWidget);
  });

  testWidgets('cache cleanup failure retains usage and supports retry', (
    tester,
  ) async {
    final api = CacheSettingsApi()..failClear = true;
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: ImageCacheCard(api: api)),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('清除图片缓存'));
    await tester.pumpAndSettle();
    expect(find.text('图片缓存清理失败，请重试'), findsOneWidget);
    expect(find.text('2.0 MB · 3 张图片'), findsOneWidget);
    expect(find.textContaining('private path'), findsNothing);
    api.failClear = false;
    await tester.tap(find.text('清除图片缓存'));
    await tester.pumpAndSettle();
    expect(find.text('图片缓存已清除'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
