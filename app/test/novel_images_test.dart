import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:qiye_library/novel_blocks.dart';
import 'package:qiye_library/novel_reader_body.dart';
import 'package:qiye_library/pixiv_direct_api.dart';
import 'package:qiye_library/models.dart';
import 'pixiv_direct_test.dart' show TokenStore, login, jsonResponse, work;

void main() {
  test('mixed blocks preserve duplicate positions and unsupported text', () {
    final blocks = novelBlocks(
      'Before[uploadedimage:2]After[uploadedimage:2][unknown:3]',
      {'uploadedimage:2': 'https://i.pximg.net/2.png'},
    );
    expect(blocks.map((b) => b.type), [
      'text',
      'image',
      'text',
      'image',
      'text',
    ]);
    expect(blocks.last.text, '[unknown:3]');
    final item = LibraryItem.fromJson({
      'id': '1',
      'assets': [
        {'id': 'text', 'mime_type': 'text/plain'},
        {
          'id': 'image',
          'mime_type': 'image/png',
          'inline_key': 'uploadedimage:2',
        },
      ],
    });
    expect(item.assets.length, 2);
    expect(item.readingAssets.single.id, 'text');
  });

  test(
    'Pixiv direct reads uploaded images and artwork page selectors safely',
    () async {
      final requests = <String>[];
      final api = PixivDirectApi(
        scope: 'synthetic.test',
        store: TokenStore(),
        client: MockClient((request) async {
          requests.add(request.url.path);
          if (request.url.host == 'oauth.secure.pixiv.net') return login();
          if (request.url.path == '/v1/illust/detail') {
            return jsonResponse({'illust': work(3)});
          }
          return http.Response(
            'novel: ${jsonEncode({
              'id': '1',
              'text': 'Before[uploadedimage:2][pixivimage:3][uploadedimage:4]',
              'images': {
                '2': {
                  'urls': {'original': 'https://i.pximg.net/upload.png'},
                },
                '4': {
                  'urls': {'original': 'https://evil.test/no.png'},
                },
              },
            })}, isOwnWork: false',
            200,
          );
        }),
      );
      await api.connect('synthetic-refresh-token');
      final document = await api.novelDocument('1');
      expect(
        document.blocks.where((b) => b.type == 'image').map((b) => b.url),
        ['https://i.pximg.net/upload.png', 'https://i.pximg.net/3.png', null],
      );
      expect(requests.where((v) => v == '/v1/illust/detail').length, 1);
      api.close();
    },
  );

  testWidgets(
    'inline image readiness never changes novel scroll extent or saved ratio',
    (tester) async {
      final controller = ScrollController();
      final loaded = ValueNotifier(false);
      final blocks = [
        for (var i = 0; i < 80; i++) ...[
          NovelBlock(text: 'Paragraph $i text.'),
          NovelBlock(type: 'image', key: '$i', width: 600, height: 900),
        ],
      ];
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: NovelReaderBody(
              text: '',
              title: 'Synthetic chapter',
              blocks: blocks,
              fontSize: 19,
              restoreOffset: 1,
              controller: controller,
              footer: const Text('End'),
              onLayoutStart: () {},
              onLayoutReady: () {},
              imageBuilder: (image) => ValueListenableBuilder(
                valueListenable: loaded,
                builder: (_, value, _) =>
                    ColoredBox(color: value ? Colors.blue : Colors.grey),
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      final extent = controller.position.maxScrollExtent;
      expect(controller.offset, extent);
      loaded.value = true;
      await tester.pumpAndSettle();
      expect(controller.position.maxScrollExtent, extent);
      await tester.drag(
        find.byType(SingleChildScrollView),
        const Offset(0, 500),
      );
      await tester.pumpAndSettle();
      expect(controller.offset, lessThan(extent));
      expect(controller.position.maxScrollExtent, extent);
      expect(find.byType(ColoredBox).evaluate().length, lessThan(8));
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
      controller.dispose();
      loaded.dispose();
    },
  );
}
