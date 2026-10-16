import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/platform_endpoints_screen.dart';

Map<String, dynamic> endpoint({bool enabled = false, String? custom}) => {
  'platform': 'kemono',
  'label': 'Kemono',
  'default_url': 'https://kemono.cr/api/v1',
  'custom_url': custom,
  'enabled': enabled,
  'effective_url': enabled ? custom : 'https://kemono.cr/api/v1',
  'description': '作者与作品 API 地址。',
};

http.Response jsonResponse(
  String body,
  int status, {
  Map<String, String>? headers,
}) => http.Response(
  body,
  status,
  headers: {'content-type': 'application/json; charset=utf-8', ...?headers},
);

Future<void> settleHttp(WidgetTester tester) async {
  await tester.pump();
  await tester.runAsync(
    () async => Future<void>.delayed(const Duration(milliseconds: 80)),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets(
    'custom API changes only platform configuration on the bound server',
    (tester) async {
      Map<String, dynamic>? submitted;
      final api = LibraryApi(
        server: Uri.parse('https://library.test'),
        token: 'device-test',
        client: MockClient((request) async {
          expect(request.url.host, 'library.test');
          expect(request.headers['authorization'], 'Bearer device-test');
          if (request.method == 'GET') {
            return jsonResponse(
              jsonEncode({
                'items': [endpoint()],
              }),
              200,
            );
          }
          expect(request.url.path, '/api/platform-endpoints/kemono');
          submitted = jsonDecode(request.body) as Map<String, dynamic>;
          return jsonResponse(
            jsonEncode(
              endpoint(
                enabled: true,
                custom: submitted!['custom_url'] as String,
              ),
            ),
            200,
          );
        }),
      );
      addTearDown(api.close);
      await tester.pumpWidget(
        MaterialApp(home: PlatformEndpointsScreen(api: api)),
      );
      await settleHttp(tester);
      await tester.tap(find.text('Kemono'));
      await settleHttp(tester);
      await tester.tap(find.byType(SwitchListTile));
      await tester.enterText(
        find.byType(TextFormField).at(0),
        'https://mirror.example/api/v1',
      );
      await tester.enterText(find.byType(TextFormField).at(1), 'admin-test');
      await tester.ensureVisible(find.text('保存设置'));
      await tester.tap(find.text('保存设置'));
      await settleHttp(tester);
      expect(submitted, {
        'custom_url': 'https://mirror.example/api/v1',
        'enabled': true,
        'admin_password': 'admin-test',
      });
      expect(
        find.textContaining('自定义接口\nhttps://mirror.example/api/v1'),
        findsOneWidget,
      );
      expect(api.server.toString(), 'https://library.test');
    },
  );

  testWidgets(
    'failed admin validation retains edits and device binding for retry',
    (tester) async {
      var unauthorized = false, puts = 0;
      final api = LibraryApi(
        server: Uri.parse('https://library.test'),
        token: 'device-test',
        onUnauthorized: () => unauthorized = true,
        client: MockClient((request) async {
          if (request.method == 'GET') {
            return jsonResponse(
              jsonEncode({
                'items': [endpoint()],
              }),
              200,
            );
          }
          puts++;
          return jsonResponse(
            jsonEncode({'detail': '管理员密码错误；设备登录仍然有效'}),
            403,
            headers: {'content-type': 'application/json; charset=utf-8'},
          );
        }),
      );
      addTearDown(api.close);
      await tester.pumpWidget(
        MaterialApp(home: PlatformEndpointsScreen(api: api)),
      );
      await settleHttp(tester);
      await tester.tap(find.text('Kemono'));
      await settleHttp(tester);
      await tester.tap(find.byType(SwitchListTile));
      await tester.enterText(
        find.byType(TextFormField).at(0),
        'https://mirror.example/api/v1',
      );
      await tester.enterText(
        find.byType(TextFormField).at(1),
        'wrong-password',
      );
      await tester.ensureVisible(find.text('保存设置'));
      await tester.tap(find.text('保存设置'));
      await settleHttp(tester);
      expect(puts, 1);
      expect(unauthorized, false);
      expect(find.text('管理员密码错误或无权修改，请检查密码后重试'), findsOneWidget);
      expect(
        tester
            .widget<TextFormField>(find.byType(TextFormField).first)
            .controller!
            .text,
        'https://mirror.example/api/v1',
      );
      expect(find.text('保存设置'), findsOneWidget);
    },
  );

  testWidgets(
    'returning to default retains custom address and requires password',
    (tester) async {
      Map<String, dynamic>? submitted;
      final api = LibraryApi(
        server: Uri.parse('https://library.test'),
        token: 'device-test',
        client: MockClient((request) async {
          if (request.method == 'GET') {
            return jsonResponse(
              jsonEncode({
                'items': [
                  endpoint(
                    enabled: true,
                    custom: 'https://mirror.example/api/v1',
                  ),
                ],
              }),
              200,
            );
          }
          submitted = jsonDecode(request.body) as Map<String, dynamic>;
          return jsonResponse(
            jsonEncode(endpoint(custom: submitted!['custom_url'] as String)),
            200,
          );
        }),
      );
      addTearDown(api.close);
      await tester.pumpWidget(
        MaterialApp(home: PlatformEndpointsScreen(api: api)),
      );
      await settleHttp(tester);
      await tester.tap(find.text('Kemono'));
      await settleHttp(tester);
      await tester.ensureVisible(find.text('使用默认接口'));
      await tester.tap(find.text('使用默认接口'));
      await tester.pump();
      await tester.ensureVisible(find.text('保存设置'));
      await tester.tap(find.text('保存设置'));
      await settleHttp(tester);
      expect(submitted, null);
      expect(find.text('请输入管理员密码'), findsOneWidget);
      await tester.enterText(find.byType(TextFormField).at(1), 'admin-test');
      await tester.ensureVisible(find.text('保存设置'));
      await tester.tap(find.text('保存设置'));
      await settleHttp(tester);
      expect(submitted!['enabled'], false);
      expect(submitted!['custom_url'], 'https://mirror.example/api/v1');
      expect(
        find.textContaining('默认接口\nhttps://kemono.cr/api/v1'),
        findsOneWidget,
      );
    },
  );
}
