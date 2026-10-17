import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/author_download_controls.dart';

class DownloadApi extends LibraryApi {
  final requests = <Map<String, dynamic>>[];
  bool fail = false;
  DownloadApi() : super(server: Uri.parse('http://192.168.1.20:8800'));
  @override
  Future<dynamic> put(String path, [Map<String, dynamic>? body]) async {
    requests.add({'path': path, ...body!});
    if (fail) throw const ApiFailure('合成保存失败');
    return {...body, 'subscribed': true};
  }
}

void main() {
  testWidgets('作者发布与公开收藏两个开关独立保存并保留另一项', (tester) async {
    final api = DownloadApi();
    addTearDown(api.close);
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: AuthorDownloadControls(
            api: api,
            authorId: '77',
            initial: const {
              'download_works': false,
              'download_bookmarks': true,
            },
          ),
        ),
      ),
    );
    await tester.tap(find.text('下载作者作品'));
    await tester.pumpAndSettle();
    expect(api.requests.last, {
      'path': '/pixiv/authors/77/download-settings',
      'download_works': true,
      'download_bookmarks': true,
    });
    await tester.tap(find.text('下载作者公开收藏'));
    await tester.pumpAndSettle();
    expect(api.requests.last['download_works'], true);
    expect(api.requests.last['download_bookmarks'], false);
  });
  testWidgets('保存失败不会改变作者下载开关', (tester) async {
    final api = DownloadApi()..fail = true;
    addTearDown(api.close);
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: AuthorDownloadControls(
            api: api,
            authorId: '77',
            initial: const {
              'download_works': false,
              'download_bookmarks': false,
            },
          ),
        ),
      ),
    );
    await tester.tap(find.text('下载作者作品'));
    await tester.pumpAndSettle();
    expect(find.text('合成保存失败'), findsOneWidget);
    expect(
      tester
          .widgetList<FilterChip>(find.byType(FilterChip))
          .every((chip) => !chip.selected),
      true,
    );
  });
}
