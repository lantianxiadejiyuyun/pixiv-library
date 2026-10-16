import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:video_player_platform_interface/video_player_platform_interface.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/models.dart';
import 'package:qiye_library/reader_screen.dart';
import 'package:qiye_library/video_asset_player.dart';

class FakeVideoPlatform extends VideoPlayerPlatform {
  final sources = <DataSource>[];
  final streams = <int, StreamController<VideoEvent>>{};
  final positions = <int, Duration>{};
  final pauses = <int>[];
  final plays = <int>[];
  final disposed = <int>[];
  final seeks = <(int, Duration)>[];
  bool failNext = false, holdInitialization = false;
  @override
  Future<void> init() async {}
  @override
  Future<int?> createWithOptions(VideoCreationOptions options) async {
    final id = sources.length;
    sources.add(options.dataSource);
    streams[id] = StreamController<VideoEvent>();
    if (failNext) {
      failNext = false;
      streams[id]!.addError(
        PlatformException(
          code: 'error',
          message: 'private-token native detail',
        ),
      );
    } else if (!holdInitialization) {
      initialize(id);
    }
    return id;
  }

  void initialize(int id) => streams[id]!.add(
    VideoEvent(
      eventType: VideoEventType.initialized,
      duration: const Duration(seconds: 100),
      size: const Size(1280, 720),
    ),
  );
  @override
  Stream<VideoEvent> videoEventsFor(int playerId) => streams[playerId]!.stream;
  @override
  Future<void> dispose(int playerId) async {
    disposed.add(playerId);
    await streams[playerId]?.close();
  }

  @override
  Future<void> pause(int playerId) async {
    pauses.add(playerId);
  }

  @override
  Future<void> play(int playerId) async {
    plays.add(playerId);
  }

  @override
  Future<Duration> getPosition(int playerId) async =>
      positions[playerId] ?? Duration.zero;
  @override
  Future<void> seekTo(int playerId, Duration position) async {
    seeks.add((playerId, position));
    positions[playerId] = position;
  }

  @override
  Future<void> setLooping(int playerId, bool looping) async {}
  @override
  Future<void> setVolume(int playerId, double volume) async {}
  @override
  Future<void> setPlaybackSpeed(int playerId, double speed) async {}
  @override
  Widget buildViewWithOptions(VideoViewOptions options) => ColoredBox(
    key: ValueKey('native-video-${options.playerId}'),
    color: Colors.black,
  );
}

class VideoApi extends LibraryApi {
  VideoApi()
    : super(server: Uri.parse('https://library.test'), token: 'device-secret');
  final saves = <Map<String, dynamic>>[];
  @override
  Future<dynamic> put(String path, [Map<String, dynamic>? body]) async {
    saves.add({'path': path, ...?body});
    return body;
  }
}

LibraryItem videoBook({double offset = .4, int count = 1}) =>
    LibraryItem.fromJson({
      'id': 'video-book',
      'title': '视频作品',
      'content_state': 'ready',
      'page_count': count,
      'progress': {'page': 0, 'section': 0, 'offset': offset},
      'assets': [
        for (int i = 0; i < count; i++)
          {
            'id': 'video-$i',
            'position': i,
            'mime_type': 'video/mp4',
            'original_name': '视频 ${i + 1}',
          },
      ],
    });

Future<void> settleVideo(WidgetTester tester) async {
  for (int i = 0; i < 6; i++) {
    await tester.pump();
  }
  await tester.runAsync(() => Future<void>.delayed(Duration.zero));
  await tester.pump();
}

void main() {
  late FakeVideoPlatform platform;
  setUp(() {
    FlutterSecureStorage.setMockInitialValues({});
    platform = FakeVideoPlatform();
    VideoPlayerPlatform.instance = platform;
  });

  testWidgets(
    'native video uses same-server URL and bearer headers, restores only after initialization',
    (tester) async {
      final api = VideoApi();
      final updates = <double>[];
      platform.holdInitialization = true;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: VideoAssetPlayer(
              api: api,
              assetId: 'video-0',
              active: true,
              initialOffset: .4,
              onPosition: (offset, _) => updates.add(offset),
            ),
          ),
        ),
      );
      await settleVideo(tester);
      expect(
        platform.sources.single.uri,
        'https://library.test/api/assets/video-0/content',
      );
      expect(platform.sources.single.httpHeaders, {
        'Authorization': 'Bearer device-secret',
      });
      expect(platform.sources.single.uri, isNot(contains('secret')));
      expect(updates, isEmpty);
      platform.initialize(0);
      await settleVideo(tester);
      expect(platform.seeks, contains((0, const Duration(seconds: 40))));
      expect(updates.last, .4);
      expect(platform.plays, isEmpty);
      await tester.pumpWidget(const SizedBox());
      await settleVideo(tester);
      expect(platform.disposed, [0]);
    },
  );

  testWidgets(
    'only active video creates a player and failure retry is local and redacted',
    (tester) async {
      final api = VideoApi();
      final updates = <double>[];
      Widget screen(bool active) => MaterialApp(
        home: Scaffold(
          body: VideoAssetPlayer(
            api: api,
            assetId: 'video-0',
            active: active,
            initialOffset: .6,
            onPosition: (offset, _) => updates.add(offset),
          ),
        ),
      );
      await tester.pumpWidget(screen(false));
      expect(platform.sources, isEmpty);
      platform.failNext = true;
      await tester.pumpWidget(screen(true));
      await settleVideo(tester);
      expect(find.text('重试视频'), findsOneWidget);
      expect(find.textContaining('private-token'), findsNothing);
      expect(updates, isEmpty);
      await tester.tap(find.text('重试视频'));
      await settleVideo(tester);
      expect(platform.sources, hasLength(2));
      expect(platform.seeks, contains((1, const Duration(seconds: 60))));
      expect(updates.last, .6);
      await tester.pumpWidget(screen(false));
      await settleVideo(tester);
      expect(platform.disposed, containsAll([0, 1]));
    },
  );

  testWidgets(
    'leaving during initialization releases native player without writing progress',
    (tester) async {
      final api = VideoApi();
      final updates = <double>[];
      platform.holdInitialization = true;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: VideoAssetPlayer(
              api: api,
              assetId: 'video-0',
              active: true,
              initialOffset: .8,
              onPosition: (offset, _) => updates.add(offset),
            ),
          ),
        ),
      );
      await settleVideo(tester);
      expect(platform.sources, hasLength(1));
      await tester.pumpWidget(const SizedBox());
      await settleVideo(tester);
      expect(platform.disposed, [0]);
      expect(updates, isEmpty);
    },
  );

  testWidgets(
    'play seek fullscreen share one native player and background pauses',
    (tester) async {
      final api = VideoApi();
      final updates = <double>[];
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: VideoAssetPlayer(
              api: api,
              assetId: 'video-0',
              active: true,
              onPosition: (offset, _) => updates.add(offset),
            ),
          ),
        ),
      );
      await settleVideo(tester);
      await tester.tap(find.byTooltip('播放视频'));
      await settleVideo(tester);
      expect(platform.plays, [0]);
      tester.widget<Slider>(find.byType(Slider)).onChanged!(.7);
      await settleVideo(tester);
      expect(platform.seeks.last, (0, const Duration(seconds: 70)));
      expect(updates.last, .7);
      await tester.tap(find.byTooltip('全屏播放'));
      await tester.pumpAndSettle();
      expect(find.byTooltip('退出全屏'), findsOneWidget);
      expect(platform.sources, hasLength(1));
      await tester.tap(find.byTooltip('退出全屏'));
      await tester.pumpAndSettle();
      final pauses = platform.pauses.length;
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      await settleVideo(tester);
      expect(platform.pauses.length, greaterThan(pauses));
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pumpWidget(const SizedBox());
      await settleVideo(tester);
      expect(platform.disposed, [0]);
    },
  );

  testWidgets(
    'reader preserves old progress on load failure and retries from original offset',
    (tester) async {
      final api = VideoApi();
      platform.failNext = true;
      await tester.pumpWidget(
        MaterialApp(
          home: ReaderScreen(api: api, item: videoBook()),
        ),
      );
      await tester.pumpAndSettle();
      await tester.pump(const Duration(seconds: 6));
      expect(api.saves, isEmpty);
      await tester.tap(find.text('重试视频'));
      await settleVideo(tester);
      expect(platform.seeks.last, (1, const Duration(seconds: 40)));
      await tester.pump(const Duration(seconds: 5));
      await settleVideo(tester);
      expect(api.saves.last['offset'], .4);
      expect(api.saves.last['section'], 0);
      await tester.pumpWidget(const SizedBox());
      await settleVideo(tester);
    },
  );

  testWidgets(
    'reader paging flushes current native position and activates only next video',
    (tester) async {
      final api = VideoApi();
      await tester.pumpWidget(
        MaterialApp(
          home: ReaderScreen(api: api, item: videoBook(count: 2)),
        ),
      );
      await tester.pumpAndSettle();
      expect(platform.sources, hasLength(1));
      platform.positions[0] = const Duration(seconds: 73);
      await tester.tap(find.byTooltip('下一 P'));
      await tester.pumpAndSettle();
      await settleVideo(tester);
      expect(
        api.saves.any((save) => save['page'] == 0 && save['offset'] == .73),
        isTrue,
      );
      expect(platform.disposed, contains(0));
      expect(platform.sources, hasLength(2));
      expect(platform.sources.last.uri, endsWith('/video-1/content'));
      expect(platform.plays, isEmpty);
      await tester.pumpWidget(const SizedBox());
      await settleVideo(tester);
    },
  );

  testWidgets(
    'leaving reader flushes latest video position then disposes playback',
    (tester) async {
      final api = VideoApi();
      await tester.pumpWidget(
        MaterialApp(
          home: Builder(
            builder: (context) => Scaffold(
              body: TextButton(
                onPressed: () => Navigator.of(context).push(
                  MaterialPageRoute<void>(
                    builder: (_) => ReaderScreen(api: api, item: videoBook()),
                  ),
                ),
                child: const Text('打开视频'),
              ),
            ),
          ),
        ),
      );
      await tester.tap(find.text('打开视频'));
      await tester.pumpAndSettle();
      platform.positions[0] = const Duration(seconds: 82);
      await tester.tap(find.byTooltip('返回书库'));
      await tester.pumpAndSettle();
      await settleVideo(tester);
      expect(api.saves.last['offset'], .82);
      expect(find.text('打开视频'), findsOneWidget);
      expect(platform.disposed, [0]);
    },
  );

  test('media authentication rejects expired or closed sessions', () {
    final expired = LibraryApi(
      server: Uri.parse('https://library.test'),
      token: 'private',
      expiresAt: DateTime.utc(2020),
    );
    expect(expired.mediaHeaders, throwsA(isA<ApiFailure>()));
    final closed = VideoApi()..close();
    expect(closed.mediaHeaders, throwsA(isA<ApiFailure>()));
  });
}
