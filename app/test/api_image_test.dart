import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/api_image.dart';
import 'package:qiye_library/app_image_cache.dart';

void main() {
  testWidgets(
    'decode failure retry evicts poisoned bytes and fetches a working image',
    (tester) async {
      var requests = 0;
      final cache = AppImageCache(
        directoryProvider: () async =>
            throw const FileSystemException('no disk in widget test'),
      );
      final api = LibraryApi(
        server: Uri.parse('https://library.test'),
        imageCache: cache,
        client: MockClient((_) async {
          requests++;
          return http.Response.bytes(
            requests == 1
                ? [0, 1, 2]
                : base64Decode(
                    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
                  ),
            200,
          );
        }),
      );
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ApiImage(api: api, path: '/items/book/cover'),
          ),
        ),
      );
      await tester.pump();
      await tester.runAsync(
        () async => Future<void>.delayed(const Duration(milliseconds: 80)),
      );
      await tester.pumpAndSettle();
      expect(find.byTooltip('重新加载图片'), findsOneWidget);
      expect(requests, 1);
      await tester.tap(find.byTooltip('重新加载图片'));
      await tester.pump();
      await tester.runAsync(
        () async => Future<void>.delayed(const Duration(milliseconds: 80)),
      );
      await tester.pumpAndSettle();
      expect(requests, 2);
      expect(find.byTooltip('重新加载图片'), findsNothing);
      expect(find.byType(Image), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
      api.close();
    },
  );
}
