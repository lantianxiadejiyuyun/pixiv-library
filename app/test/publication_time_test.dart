import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:qiye_library/models.dart';
import 'package:qiye_library/publication_time.dart';
import 'package:qiye_library/pixiv_direct_api.dart';
import 'package:qiye_library/pixiv_direct_screen.dart';
import 'pixiv_direct_test.dart' show TokenStore, login, work;

void main() {
  test(
    'original publication is parsed as an instant and survives local preference changes',
    () {
      final item = LibraryItem.fromJson({
        'id': 'test',
        'source_published_at': '2020-02-29T08:15:20+09:00',
        'created_at': '2026-10-17T00:00:00Z',
      });
      final expected = DateTime.utc(2020, 2, 28, 23, 15, 20);
      expect(item.sourcePublishedAt, expected);
      expect(item.withScore(5).sourcePublishedAt, expected);
      expect(
        item.withFavorite({
          'is_favorite': true,
          'favorite_group_ids': [],
        }).sourcePublishedAt,
        expected,
      );
      expect(
        item.withProgress(const ReadingPosition(page: 2)).sourcePublishedAt,
        expected,
      );
      expect(publicationTime(expected), contains('2020-'));
    },
  );
  for (final value in [
    null,
    'bad',
    '2020-02-30T00:00:00Z',
    '2020-01-01',
    '2020-01-01T00:00:00',
    '2020-01-01T00:00:00+25:00',
  ]) {
    test('missing or invalid original date shows unknown: $value', () {
      expect(parsePublicationTime(value), isNull);
      expect(publicationTime(parsePublicationTime(value)), '暂无');
      expect(
        LibraryItem.fromJson({
          'id': 'test',
          'created_at': '2026-10-17T00:00:00Z',
          'source_published_at': value,
        }).sourcePublishedAt,
        isNull,
      );
    });
  }
  testWidgets('direct Pixiv work shows original publication in native detail', (
    tester,
  ) async {
    final api = PixivDirectApi(
      store: TokenStore(),
      scope: 'test',
      client: MockClient((request) async {
        if (request.url.host == 'oauth.secure.pixiv.net') return login();
        return http.Response(
          jsonEncode({
            'illust': {...work(42), 'create_date': '2020-02-29T08:15:20+09:00'},
          }),
          200,
        );
      }),
    );
    addTearDown(api.close);
    await tester.runAsync(() => api.connect('refresh-test-token'));
    await tester.pumpWidget(
      MaterialApp(
        home: PixivOnlineWorkScreen(api: api, id: '42', novel: false),
      ),
    );
    for (
      var i = 0;
      i < 30 && find.textContaining('原作者发布时间').evaluate().isEmpty;
      i++
    ) {
      await tester.runAsync(() async {
        await Future<void>.delayed(const Duration(milliseconds: 5));
      });
      await tester.pump(const Duration(milliseconds: 10));
    }
    expect(
      find.text(
        '原作者发布时间：${publicationTime(DateTime.utc(2020, 2, 28, 23, 15, 20))}',
      ),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const MaterialApp(home: SizedBox()));
  });
}
