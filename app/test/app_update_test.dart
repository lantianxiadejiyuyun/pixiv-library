import 'dart:io';
import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/app_update.dart';

Map<String, dynamic> release({int code = 5}) => {
  'version_code': code,
  'version_name': '0.5.0',
  'min_sdk': 24,
  'size_bytes': 3,
  'sha256': 'a' * 64,
  'certificate_sha256': 'b' * 64,
  'package_name': 'app.qiye.qiye_library',
  'notes': '更新说明',
  'download_path': '/api/app-updates/android/${'a' * 64}/apk',
};

class UpdateApi extends LibraryApi {
  Map? remote = release();
  UpdateApi() : super(server: Uri.parse('https://library.test'));
  @override
  Future<dynamic> get(String path, {Map<String, String>? query}) async => {
    'release': remote,
  };
}

void main() {
  test(
    'manifest rejects external resources, wrong package and unbounded sizes',
    () {
      expect(AndroidUpdate.fromJson(release()).code, 5);
      for (final bad in [
        {...release(), 'download_path': 'https://evil.test/a.apk'},
        {...release(), 'package_name': 'evil.app'},
        {...release(), 'size_bytes': 257 * 1024 * 1024},
        {...release(), 'version_code': -1},
        {...release(), 'sha256': 'invalid'},
      ]) {
        expect(() => AndroidUpdate.fromJson(bad), throwsA(isA<ApiFailure>()));
      }
    },
  );
  test('APK streams authenticated bytes and verifies SHA256', () async {
    final directory = await Directory.systemTemp.createTemp('qiye-update-test');
    addTearDown(() => directory.delete(recursive: true));
    final payload = Uint8List.fromList([1, 2, 3]);
    final hash = sha256.convert(payload).toString();
    final api = LibraryApi(
      server: Uri.parse('https://library.test'),
      token: 'secret',
      client: MockClient((request) async {
        expect(request.headers['Authorization'], 'Bearer secret');
        expect(request.followRedirects, false);
        expect(request.url.host, 'library.test');
        return http.Response.bytes(payload, 200);
      }),
    );
    final target = File('${directory.path}/update.apk');
    var progress = 0;
    await api.downloadApk(
      '/api/app-updates/android/$hash/apk',
      target,
      size: 3,
      digest: hash,
      onProgress: (value) => progress = value,
    );
    expect(await target.readAsBytes(), payload);
    expect(progress, 3);
  });
  for (final status in [200, 302, 401]) {
    test(
      'bad hash, redirects and revoked credentials leave no partial APK: $status',
      () async {
        final directory = await Directory.systemTemp.createTemp(
          'qiye-update-test',
        );
        addTearDown(() => directory.delete(recursive: true));
        var unauthorized = false;
        final api = LibraryApi(
          server: Uri.parse('https://library.test'),
          token: 'secret',
          onUnauthorized: () => unauthorized = true,
          client: MockClient(
            (request) async => http.Response.bytes([1, 2, 3], status),
          ),
        );
        final file = File('${directory.path}/update.apk');
        await expectLater(
          api.downloadApk(
            '/api/app-updates/android/${'a' * 64}/apk',
            file,
            size: 3,
            digest: 'a' * 64,
            onProgress: (_) {},
          ),
          throwsA(isA<ApiFailure>()),
        );
        expect(await file.exists(), false);
        expect(unauthorized, status == 401);
      },
    );
  }
  testWidgets(
    'updater compares installed version and rejects mismatched signature',
    (tester) async {
      debugDefaultTargetPlatformOverride = TargetPlatform.android;
      addTearDown(() => debugDefaultTargetPlatformOverride = null);
      const channel = MethodChannel('app.qiye/updater');
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(
            channel,
            (call) async => {
              'version_name': '0.4.0',
              'version_code': 4,
              'sdk': 36,
              'certificates': ['b' * 64],
            },
          );
      addTearDown(
        () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
            .setMockMethodCallHandler(channel, null),
      );
      final api = UpdateApi();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: AppUpdateCard(api: api)),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('当前版本 0.4.0（4）'), findsOneWidget);
      await tester.tap(find.text('检查更新'));
      await tester.pumpAndSettle();
      expect(find.text('下载更新'), findsOneWidget);
      api.remote = release(code: 4);
      await tester.tap(find.text('检查更新'));
      await tester.pumpAndSettle();
      expect(find.text('已是最新版本'), findsOneWidget);
      api.remote = {...release(), 'certificate_sha256': 'c' * 64};
      await tester.tap(find.text('检查更新'));
      await tester.pumpAndSettle();
      expect(find.text('新版签名与已安装客户端不一致'), findsOneWidget);
      expect(find.text('下载更新'), findsNothing);
    debugDefaultTargetPlatformOverride = null;
    },
  );
}
