import 'dart:convert';
import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:qiye_library/admin_screen.dart';
import 'package:qiye_library/api.dart';

http.Response response(
  dynamic value, [
  int code = 200,
  Map<String, String> extra = const {},
]) => http.Response(
  jsonEncode(value),
  code,
  headers: {'content-type': 'application/json; charset=utf-8', ...extra},
);

JsonMap action(
  String method,
  String path,
  String title, {
  JsonMap? body,
  List<JsonMap> parameters = const [],
  bool confirmation = false,
}) => {
  'id': '$method $path',
  'method': method,
  'path': path,
  'title': title,
  'group': 'queue',
  'parameters': parameters,
  'body': body,
  'media_type': body == null ? null : 'application/json',
  'download': false,
  'confirmation': confirmation,
};

final queueBody = <String, dynamic>{
  'type': 'object',
  'properties': {
    'paused': {'type': 'boolean', 'nullable': true},
    'concurrency': {
      'type': 'integer',
      'minimum': 1,
      'maximum': 8,
      'nullable': true,
    },
  },
};
final readQueue = action('GET', '/queue/settings', '查看采集设置');
final writeQueue = action('PUT', '/queue/settings', '修改采集设置', body: queueBody);
JsonMap fixture({List<JsonMap>? actions}) => {
  'version': 1,
  'groups': {'queue': '任务与队列'},
  'labels': {
    'paused': '暂停',
    'concurrency': '并发数',
    'credentials': '账号凭据',
    'name': '名称',
    'subscription_id': '订阅',
  },
  'actions': actions ?? [readQueue, writeQueue],
};

Future<void> settle(WidgetTester tester) async {
  await tester.pump();
  await tester.runAsync(
    () async => Future<void>.delayed(const Duration(milliseconds: 80)),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('retry permission uses a multi-account Pixiv picker', (
    tester,
  ) async {
    final requests = <String>[];
    final api = LibraryApi(
      server: Uri.parse('https://library.test'),
      client: MockClient((request) async {
        requests.add(request.url.path);
        return response([
          {
            'platform': 'pixiv',
            'accounts': [
              {'id': 'main', 'label': '主账号'},
              {'id': 'helper', 'label': '副账号'},
            ],
          },
          {
            'platform': 'kemono',
            'accounts': [
              {'id': 'other', 'label': '其他平台账号'},
            ],
          },
        ]);
      }),
    );
    addTearDown(api.close);
    final catalog = AdminCatalog.fromJson({
      ...fixture(),
      'labels': {'retry_account_ids': '失败重试账号'},
    });
    dynamic selected = ['main'];
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: StatefulBuilder(
            builder: (_, change) => AdminField(
              api: api,
              catalog: catalog,
              name: 'retry_account_ids',
              required: false,
              schema: {
                'type': 'array',
                'items': {'type': 'string'},
              },
              resource: '/sources/pixiv/account-roles',
              value: selected,
              onChanged: (value) => change(() => selected = value),
            ),
          ),
        ),
      ),
    );
    await tester.tap(find.text('选择失败重试账号（1）'));
    await settle(tester);
    expect(requests, ['/api/sources']);
    expect(find.text('Pixiv · 主账号'), findsOneWidget);
    expect(find.text('Pixiv · 副账号'), findsOneWidget);
    expect(find.textContaining('其他平台账号'), findsNothing);
    final main = tester.widget<CheckboxListTile>(
      find.widgetWithText(CheckboxListTile, 'Pixiv · 主账号'),
    );
    expect(main.value, true);
    await tester.tap(find.text('Pixiv · 副账号'));
    await tester.tap(find.text('完成'));
    await settle(tester);
    expect(selected, ['main', 'helper']);
    expect(find.text('选择失败重试账号（2）'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'retry lane edits its own pause and concurrency while collection stays paused',
    (tester) async {
      final writes = <(String, JsonMap)>[];
      final api = LibraryApi(
        server: Uri.parse('https://library.test'),
        client: MockClient((request) async {
          if (request.method == 'GET') {
            expect(request.url.path, '/api/queue/status');
            return response({
              'settings': {'paused': true, 'concurrency': 1},
              'lanes': [
                {'id': 'retry', 'paused': false, 'concurrency': 2},
                {'id': 'download', 'paused': false, 'concurrency': 1},
              ],
            });
          }
          final body = jsonDecode(request.body) as JsonMap;
          writes.add((request.url.path, body));
          return response(body);
        }),
      );
      addTearDown(api.close);
      final edit = action(
        'PUT',
        '/queue/lanes/{lane}',
        '修改队列',
        body: queueBody,
        parameters: [
          {
            'name': 'lane',
            'location': 'path',
            'required': true,
            'schema': {
              'type': 'string',
              'enum': ['download', 'retry'],
            },
          },
        ],
      );
      await tester.pumpWidget(
        MaterialApp(
          home: AdminOperationPage(
            api: api,
            catalog: AdminCatalog.fromJson(
              fixture(actions: [action('GET', '/queue/status', '查看队列'), edit]),
            ),
            action: edit,
            initial: {'lane': 'retry'},
          ),
        ),
      );
      await settle(tester);
      expect(find.text('失败重试'), findsOneWidget);
      expect(find.text('2'), findsOneWidget);
      final pause = find.byType(DropdownButtonFormField<bool>);
      expect(
        tester.widget<DropdownButtonFormField<bool>>(pause).initialValue,
        false,
      );
      await tester.tap(pause);
      await tester.pumpAndSettle();
      await tester.tap(find.text('是').last);
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextFormField), '4');
      await tester.ensureVisible(find.bySubtype<FilledButton>());
      await tester.tap(find.bySubtype<FilledButton>());
      await settle(tester);
      expect(writes, hasLength(1));
      expect(writes.single.$1, '/api/queue/lanes/retry');
      expect(writes.single.$2, {'paused': true, 'concurrency': 4});
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('AI node picker uses controller nodes instead of proxy nodes', (
    tester,
  ) async {
    final requests = <String>[];
    final api = LibraryApi(
      server: Uri.parse('https://library.test'),
      client: MockClient((request) async {
        requests.add(request.url.path);
        return response({
          'items': [
            {'id': 'ai-node', 'label': 'AI Computer', 'enabled': true},
          ],
        });
      }),
    );
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: AdminEntityPicker(
            api: api,
            catalog: AdminCatalog.fromJson(fixture()),
            name: 'node_id',
            resource: '/ai/nodes/{node_id}/models',
            multiple: false,
            selected: const [],
          ),
        ),
      ),
    );
    await settle(tester);
    expect(requests, ['/api/ai/nodes']);
    expect(find.textContaining('AI Computer'), findsWidgets);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
    api.close();
  });

  test(
    'administrator cookie stays separate from reader token and is dropped on close',
    () async {
      final seen = <http.Request>[];
      var expired = false;
      final admin = await LibraryApi.administrator(
        server: Uri.parse('https://library.test'),
        password: 'secret-test',
        onUnauthorized: () => expired = true,
        client: MockClient((request) async {
          seen.add(request);
          if (request.url.path == '/api/auth/login') {
            expect(request.headers['authorization'], null);
            expect(request.headers['cookie'], null);
            expect(jsonDecode(request.body), {'password': 'secret-test'});
            return response(
              {'ok': true},
              200,
              {
                'set-cookie':
                    'qiye_session=sample.signature; Max-Age=7200; HttpOnly; Path=/; SameSite=strict',
              },
            );
          }
          expect(request.headers['cookie'], 'qiye_session=sample.signature');
          expect(request.headers['authorization'], null);
          return response(
            {},
            request.url.path.endsWith('/expired') ? 401 : 200,
          );
        }),
      );
      expect(admin.isAdministrator, true);
      await admin.get('/devices');
      await expectLater(
        admin.get('/expired'),
        throwsA(isA<ApiFailure>().having((e) => e.status, 'status', 401)),
      );
      expect(expired, true);
      final reader = LibraryApi(
        server: Uri.parse('https://library.test'),
        token: 'reading-only',
        client: MockClient((r) async {
          expect(r.headers['authorization'], 'Bearer reading-only');
          expect(r.headers['cookie'], null);
          return response({});
        }),
      );
      await reader.get('/dashboard');
      reader.close();
      admin.close();
      expect(admin.isAdministrator, false);
      expect(seen.length, 3);
    },
  );

  test(
    'administrator login never follows redirects or exposes response diagnostics',
    () async {
      for (final status in [302, 401, 429]) {
        var requests = 0;
        await expectLater(
          LibraryApi.administrator(
            server: Uri.parse('https://library.test'),
            password: 'not-logged',
            client: MockClient((r) async {
              requests++;
              expect(r.followRedirects, false);
              return response(
                {'detail': 'not-logged'},
                status,
                {'location': 'https://outside.test/steal'},
              );
            }),
          ),
          throwsA(
            isA<ApiFailure>().having(
              (e) => e.message.contains('not-logged'),
              'secret hidden',
              false,
            ),
          ),
        );
        expect(requests, 1);
      }
    },
  );

  test(
    'management multipart and raw APK upload stream authenticated content',
    () async {
      final directory = await Directory.systemTemp.createTemp(
        'qiye-admin-upload-',
      );
      addTearDown(() => directory.delete(recursive: true));
      final file = await File(
        '${directory.path}/sample.txt',
      ).writeAsString('synthetic-file-content');
      final admin = await LibraryApi.administrator(
        server: Uri.parse('https://library.test'),
        password: 'test',
        client: MockClient((r) async {
          if (r.url.path.endsWith('/login')) {
            return response({}, 200, {
              'set-cookie': 'qiye_session=sample.signature; HttpOnly',
            });
          }
          expect(r.headers['cookie'], 'qiye_session=sample.signature');
          if (r.url.path.endsWith('/android')) {
            expect(r.body, 'synthetic-file-content');
            expect(
              r.headers['content-type'],
              'application/vnd.android.package-archive',
            );
          } else {
            expect(
              r.headers['content-type'],
              startsWith('multipart/form-data;'),
            );
            expect(r.body, contains('name="files"; filename="sample.txt"'));
            expect(r.body, contains('synthetic-file-content'));
          }
          return response({'ok': true});
        }),
      );
      addTearDown(admin.close);
      await admin.managementUpload(
        'POST',
        '/imports/upload',
        files: [(field: 'files', name: 'sample.txt', path: file.path)],
        fields: {'type': 'novel'},
      );
      await admin.managementUpload(
        'POST',
        '/app-updates/android',
        files: [(field: 'file', name: 'sample.apk', path: file.path)],
        raw: true,
      );
    },
  );

  testWidgets(
    'native login replaces login route and logout clears management only',
    (tester) async {
      var logouts = 0;
      final api = LibraryApi(
        server: Uri.parse('https://library.test'),
        client: MockClient((r) async {
          if (r.url.path.endsWith('/logout')) {
            logouts++;
            return response({});
          }
          return response(fixture());
        }),
      );
      await tester.pumpWidget(
        MaterialApp(
          home: AdminScreen(
            server: Uri.parse('https://library.test'),
            login: (_, password, _) async {
              expect(password, 'admin-test');
              return api;
            },
          ),
        ),
      );
      await tester.enterText(find.byType(TextFormField).last, 'admin-test');
      await tester.tap(find.text('登录管理后台'));
      await settle(tester);
      expect(find.text('管理工作台'), findsOneWidget);
      expect(find.text('任务与队列'), findsOneWidget);
      await tester.tap(find.byTooltip('退出管理员登录'));
      await settle(tester);
      expect(logouts, 1);
      expect(find.text('登录管理后台'), findsOneWidget);
      expect(find.text('admin-test'), findsNothing);
    },
  );

  testWidgets(
    'native settings preload existing values and submit typed changes',
    (tester) async {
      JsonMap? saved;
      final api = LibraryApi(
        server: Uri.parse('https://library.test'),
        client: MockClient((r) async {
          if (r.method == 'GET') {
            return response({'paused': true, 'concurrency': 4});
          }
          saved = jsonDecode(r.body) as JsonMap;
          return response(saved);
        }),
      );
      addTearDown(api.close);
      await tester.pumpWidget(
        MaterialApp(
          home: AdminOperationPage(
            api: api,
            catalog: AdminCatalog.fromJson(fixture()),
            action: writeQueue,
          ),
        ),
      );
      await settle(tester);
      expect(find.text('4'), findsOneWidget);
      await tester.enterText(find.byType(TextFormField), '6');
      await tester.ensureVisible(find.bySubtype<FilledButton>());
      await tester.tap(find.bySubtype<FilledButton>());
      await settle(tester);
      expect(saved, {'paused': true, 'concurrency': 6});
      expect(find.text('操作已完成'), findsOneWidget);
    },
  );

  testWidgets('failed settings preload cannot overwrite saved configuration', (
    tester,
  ) async {
    var writes = 0;
    final api = LibraryApi(
      server: Uri.parse('https://library.test'),
      client: MockClient((r) async {
        if (r.method != 'GET') writes++;
        return response({}, 503);
      }),
    );
    addTearDown(api.close);
    await tester.pumpWidget(
      MaterialApp(
        home: AdminOperationPage(
          api: api,
          catalog: AdminCatalog.fromJson(fixture()),
          action: writeQueue,
        ),
      ),
    );
    await settle(tester);
    await tester.ensureVisible(find.bySubtype<FilledButton>());
    await tester.tap(find.bySubtype<FilledButton>());
    await settle(tester);
    expect(writes, 0);
    expect(find.text('请先读取当前设置，确认后再保存'), findsOneWidget);
  });

  testWidgets(
    'removing a record requires confirmation and cancellation makes no request',
    (tester) async {
      var requests = 0;
      final api = LibraryApi(
        server: Uri.parse('https://library.test'),
        client: MockClient((_) async {
          requests++;
          return response({});
        }),
      );
      addTearDown(api.close);
      final remove = action(
        'DELETE',
        '/jobs/example',
        '删除测试记录',
        confirmation: true,
      );
      await tester.pumpWidget(
        MaterialApp(
          home: AdminOperationPage(
            api: api,
            catalog: AdminCatalog.fromJson(fixture()),
            action: remove,
          ),
        ),
      );
      await tester.tap(find.bySubtype<FilledButton>());
      await tester.pumpAndSettle();
      await tester.tap(find.text('取消'));
      await tester.pumpAndSettle();
      expect(requests, 0);
    },
  );

  testWidgets(
    'proxy subscription picker uses proxy subscriptions not content subscriptions',
    (tester) async {
      final paths = <String>[];
      final api = LibraryApi(
        server: Uri.parse('https://library.test'),
        client: MockClient((r) async {
          paths.add(r.url.path);
          return response({
            'subscriptions': [
              {'id': 'proxy-one', 'label': '测试代理订阅'},
            ],
          });
        }),
      );
      addTearDown(api.close);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: AdminEntityPicker(
              api: api,
              catalog: AdminCatalog.fromJson(fixture()),
              name: 'subscription_id',
              multiple: false,
              selected: const [],
              resource: '/network/managed/subscriptions/{subscription_id}',
            ),
          ),
        ),
      );
      await settle(tester);
      expect(paths, ['/api/network/managed']);
      expect(find.text('测试代理订阅'), findsOneWidget);
    },
  );

  test(
    'native defaults require explicit third-party consent and omit unset patches',
    () {
      expect(adminDefault({'type': 'boolean', 'const': true}), false);
      expect(adminClean({'title': '测试', 'tags': [], 'description': null}), {
        'title': '测试',
        'tags': [],
      });
    },
  );
}
