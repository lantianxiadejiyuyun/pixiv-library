import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/tag_tracking_screen.dart';

class TrackingApi extends LibraryApi {
  final posted = <Map<String, dynamic>>[];
  TrackingApi() : super(server: Uri.parse('http://library.test'));
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) async => {
    'items': [],
  };
  @override
  Future<dynamic> post(String path, [Map<String, dynamic>? body]) async {
    posted.add(body!);
    return {};
  }
}

void main() {
  testWidgets(
    'selected original tags create independent tracking with download enabled',
    (tester) async {
      final api = TrackingApi();
      await tester.pumpWidget(
        MaterialApp(
          home: TagTrackingScreen(
            api: api,
            tags: const ['Original tag', 'Second tag'],
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('添加标签追踪'));
      await tester.tap(find.text('添加标签追踪'));
      await tester.pumpAndSettle();
      expect(api.posted.single['tags'], ['Original tag', 'Second tag']);
      expect(api.posted.single['auto_download'], true);
      expect(api.posted.single['content_scope'], 'all');
      expect(find.text('追踪已保存，检查任务已排队'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );
}
