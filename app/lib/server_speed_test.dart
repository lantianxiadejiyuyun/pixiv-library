import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import 'api.dart';

enum SpeedTestStage {
  idle,
  latency,
  download,
  upload,
  complete,
  cancelled,
  failed,
}

class SpeedMeasurement {
  final int bytes;
  final Duration elapsed;
  const SpeedMeasurement(this.bytes, this.elapsed);

  double get megabytesPerSecond => bytes / elapsed.inMicroseconds;
  double get megabitsPerSecond => megabytesPerSecond * 8;
}

class SpeedTestSnapshot {
  final SpeedTestStage stage;
  final double? latencyMs;
  final SpeedMeasurement? download, upload;
  final int completedPings, receivedBytes, transferBytes;
  final String? error;

  const SpeedTestSnapshot({
    this.stage = SpeedTestStage.idle,
    this.latencyMs,
    this.download,
    this.upload,
    this.completedPings = 0,
    this.receivedBytes = 0,
    this.transferBytes = 8 * 1024 * 1024,
    this.error,
  });

  bool get running => const {
    SpeedTestStage.latency,
    SpeedTestStage.download,
    SpeedTestStage.upload,
  }.contains(stage);
}

class _Cancelled implements Exception {}

class _SpeedRun {
  final http.Client client;
  final Completer<void> cancelled = Completer<void>();
  final Set<void Function()> cancelListeners = {};
  bool closed = false;
  _SpeedRun(this.client);

  void close() {
    if (closed) return;
    closed = true;
    client.close();
  }

  void cancel() {
    if (!cancelled.isCompleted) {
      cancelled.complete();
      for (final listener in List.of(cancelListeners)) {
        listener();
      }
    }
    close();
  }
}

/// A bounded, uncached transfer to the currently paired server. Its own client
/// lets cancellation interrupt sockets without interrupting library requests.
class ServerSpeedTest extends ChangeNotifier {
  final LibraryApi api;
  final http.Client Function() _clientFactory;
  final Stopwatch Function() _stopwatchFactory;
  final Duration stageTimeout, idleTimeout;
  _SpeedRun? _active;
  Future<void>? _running;
  bool _disposed = false;
  int _nonce = 0;
  SpeedTestSnapshot _snapshot = const SpeedTestSnapshot();
  SpeedTestSnapshot get snapshot => _snapshot;

  ServerSpeedTest({
    required this.api,
    http.Client Function()? clientFactory,
    Stopwatch Function()? stopwatchFactory,
    this.stageTimeout = const Duration(seconds: 20),
    this.idleTimeout = const Duration(seconds: 8),
  }) : _clientFactory = clientFactory ?? http.Client.new,
       _stopwatchFactory = stopwatchFactory ?? Stopwatch.new;

  Future<void> start({int transferBytes = 8 * 1024 * 1024}) {
    if (_disposed) return Future.value();
    if (_active != null && !_active!.cancelled.isCompleted) return _running!;
    if (transferBytes < 1 || transferBytes > 32 * 1024 * 1024) {
      throw ArgumentError.value(transferBytes, 'transferBytes');
    }
    final run = _SpeedRun(_clientFactory());
    final completion = Completer<void>();
    _active = run;
    _running = completion.future;
    _snapshot = SpeedTestSnapshot(
      stage: SpeedTestStage.latency,
      transferBytes: transferBytes,
    );
    notifyListeners();
    unawaited(_execute(run, transferBytes).then(completion.complete));
    return completion.future;
  }

  void cancel() {
    final run = _active;
    if (run == null || run.cancelled.isCompleted) return;
    run.cancel();
    _update(run, stage: SpeedTestStage.cancelled);
  }

  void _update(
    _SpeedRun run, {
    SpeedTestStage? stage,
    double? latencyMs,
    SpeedMeasurement? download,
    SpeedMeasurement? upload,
    int? completedPings,
    int? receivedBytes,
    String? error,
  }) {
    if (_disposed || _active != run) return;
    _snapshot = SpeedTestSnapshot(
      stage: stage ?? _snapshot.stage,
      latencyMs: latencyMs ?? _snapshot.latencyMs,
      download: download ?? _snapshot.download,
      upload: upload ?? _snapshot.upload,
      completedPings: completedPings ?? _snapshot.completedPings,
      receivedBytes: receivedBytes ?? _snapshot.receivedBytes,
      transferBytes: _snapshot.transferBytes,
      error: error,
    );
    notifyListeners();
  }

  void _checkCancelled(_SpeedRun run) {
    if (_disposed || run.cancelled.isCompleted || _active != run) {
      throw _Cancelled();
    }
  }

  Future<T> _stage<T>(_SpeedRun run, Future<T> Function() action) async {
    _checkCancelled(run);
    return _deadline(run, action(), stageTimeout);
  }

  Future<T> _deadline<T>(
    _SpeedRun run,
    Future<T> operation,
    Duration timeout, {
    void Function(T)? onLateValue,
  }) async {
    final completion = Completer<T>();
    final timer = Timer(timeout, () {
      if (!completion.isCompleted) {
        completion.completeError(TimeoutException('speed-test'));
      }
    });
    operation.then(
      (value) {
        if (!completion.isCompleted) {
          completion.complete(value);
        } else {
          onLateValue?.call(value);
        }
      },
      onError: (Object error, StackTrace stack) {
        if (!completion.isCompleted) completion.completeError(error, stack);
      },
    );
    void cancel() {
      if (!completion.isCompleted) completion.completeError(_Cancelled());
    }

    run.cancelListeners.add(cancel);
    if (run.cancelled.isCompleted) cancel();
    try {
      return await completion.future;
    } finally {
      timer.cancel();
      run.cancelListeners.remove(cancel);
    }
  }

  Stream<List<int>> _chunks(
    _SpeedRun run,
    http.StreamedResponse response,
  ) async* {
    final iterator = StreamIterator(response.stream);
    try {
      while (await _deadline(run, iterator.moveNext(), idleTimeout)) {
        _checkCancelled(run);
        yield iterator.current;
      }
    } finally {
      await iterator.cancel();
    }
  }

  void _prepare(http.BaseRequest request) {
    request.followRedirects = false;
    request.headers.addAll(api.mediaHeaders());
    request.headers.addAll({
      'Cache-Control': 'no-store, no-cache',
      'Pragma': 'no-cache',
      'Accept-Encoding': 'identity',
    });
  }

  http.Request _get(String path, [Map<String, String> query = const {}]) =>
      http.Request(
        'GET',
        api.resolve(path, {
          ...query,
          'nonce': '${DateTime.now().microsecondsSinceEpoch}-${_nonce++}',
        }),
      );

  Future<http.StreamedResponse> _send(
    _SpeedRun run,
    http.BaseRequest request,
  ) async {
    _checkCancelled(run);
    _prepare(request);
    final response = await _deadline(
      run,
      run.client.send(request),
      request.method == 'POST' ? stageTimeout : idleTimeout,
      onLateValue: (response) {
        unawaited(response.stream.listen((_) {}).cancel());
      },
    );
    try {
      _checkCancelled(run);
      api.checkMediaAuthorization(response.statusCode);
      if (response.statusCode >= 300 && response.statusCode < 400) {
        throw const ApiFailure('测速接口发生重定向，请使用最终服务器地址重新绑定');
      }
      if (response.statusCode != 200) {
        throw ApiFailure(switch (response.statusCode) {
          404 => '服务器尚不支持测速，请先更新后端服务',
          429 => '服务器正在测速或请求过多，请稍后重试',
          413 => '测试数据超过服务器上限，请更新后端后重试',
          >= 500 => '服务器暂时不可用，请稍后重试',
          _ => '测速请求失败，请检查服务器配置',
        }, response.statusCode);
      }
      return response;
    } catch (_) {
      await response.stream.listen((_) {}).cancel();
      rethrow;
    }
  }

  Future<Map<String, dynamic>> _readJson(
    _SpeedRun run,
    http.StreamedResponse response,
  ) async {
    final body = BytesBuilder(copy: false);
    if ((response.contentLength ?? 0) > 16 * 1024) {
      await response.stream.listen((_) {}).cancel();
      throw const ApiFailure('测速响应格式不正确，请更新服务器');
    }
    await for (final chunk in _chunks(run, response)) {
      if (body.length + chunk.length > 16 * 1024) {
        throw const ApiFailure('测速响应格式不正确，请更新服务器');
      }
      body.add(chunk);
    }
    try {
      return jsonDecode(utf8.decode(body.takeBytes())) as Map<String, dynamic>;
    } catch (_) {
      throw const ApiFailure('测速响应格式不正确，请更新服务器');
    }
  }

  Duration _elapsed(Stopwatch watch) {
    watch.stop();
    final elapsed = watch.elapsed;
    if (elapsed.inMicroseconds <= 0) {
      throw const ApiFailure('测速耗时过短，请重新测试');
    }
    return elapsed;
  }

  Future<void> _execute(_SpeedRun run, int size) async {
    try {
      final pings = await _stage(run, () async {
        final values = <double>[];
        for (var i = 0; i < 5; i++) {
          final watch = _stopwatchFactory()..start();
          final response = await _send(run, _get('/network/speed/ping'));
          final data = await _readJson(run, response);
          if (data['ok'] != true) {
            throw const ApiFailure('测速响应格式不正确，请更新服务器');
          }
          _checkCancelled(run);
          values.add(_elapsed(watch).inMicroseconds / 1000);
          _update(run, completedPings: values.length);
        }
        return values;
      });
      pings.sort();
      _update(run, latencyMs: pings[2], stage: SpeedTestStage.download);
      final download = await _stage(run, () async {
        final watch = _stopwatchFactory()..start();
        final response = await _send(
          run,
          _get('/network/speed/download', {'bytes': '$size'}),
        );
        if (response.contentLength != size ||
            !(response.headers['content-type'] ?? '').startsWith(
              'application/octet-stream',
            ) ||
            !const [
              '',
              'identity',
            ].contains(response.headers['content-encoding'] ?? '')) {
          await response.stream.listen((_) {}).cancel();
          throw const ApiFailure('下载测速数据不正确，请检查服务器和反向代理配置');
        }
        var received = 0;
        var lastUpdate = Duration.zero;
        await for (final chunk in _chunks(run, response)) {
          _checkCancelled(run);
          received += chunk.length;
          if (received > size) {
            throw const ApiFailure('下载测速数据超过上限');
          }
          if (watch.elapsed - lastUpdate >= const Duration(milliseconds: 100) ||
              received == size) {
            lastUpdate = watch.elapsed;
            _update(run, receivedBytes: received);
          }
        }
        if (received != size) throw const ApiFailure('下载测速中断，收到的数据不完整');
        return SpeedMeasurement(received, _elapsed(watch));
      });
      _checkCancelled(run);
      _update(run, download: download, stage: SpeedTestStage.upload);

      // Prepare bounded data before timing. Uploads are uncompressed and stream
      // through the transport; only the server acknowledgement counts as sent.
      final random = Random();
      final block = Uint8List.fromList(
        List<int>.generate(64 * 1024, (_) => random.nextInt(256)),
      );
      final upload = await _stage(run, () async {
        final request = _UploadRequest(
          api.resolve('/network/speed/upload'),
          size,
          block,
          () => _checkCancelled(run),
        );
        final watch = _stopwatchFactory()..start();
        final response = await _send(run, request);
        final data = await _readJson(run, response);
        final received = data['received_bytes'];
        if (received is! int || received != size) {
          throw const ApiFailure('上传测速中断，服务器未确认收到完整数据');
        }
        return SpeedMeasurement(received, _elapsed(watch));
      });
      _checkCancelled(run);
      _update(run, upload: upload, stage: SpeedTestStage.complete);
    } catch (error) {
      if (error is _Cancelled || run.cancelled.isCompleted || _disposed) {
        _update(run, stage: SpeedTestStage.cancelled);
      } else {
        _update(
          run,
          stage: SpeedTestStage.failed,
          error: error is ApiFailure
              ? error.message
              : error is TimeoutException
              ? '测速超时，已保留完成的结果。请检查网络后重试'
              : '测速连接中断，请检查网络和服务器后重试',
        );
      }
    } finally {
      run.cancel();
      if (_active == run) {
        _active = null;
        _running = null;
      }
    }
  }

  @override
  void dispose() {
    _disposed = true;
    _active?.cancel();
    super.dispose();
  }
}

class _UploadRequest extends http.BaseRequest {
  final Uint8List block;
  final void Function() checkCancelled;

  _UploadRequest(Uri url, int bytes, this.block, this.checkCancelled)
    : super('POST', url) {
    contentLength = bytes;
    headers['Content-Type'] = 'application/octet-stream';
  }

  @override
  http.ByteStream finalize() {
    super.finalize();
    Stream<List<int>> chunks() async* {
      for (var offset = 0; offset < contentLength!; offset += block.length) {
        checkCancelled();
        yield offset + block.length <= contentLength!
            ? block
            : Uint8List.sublistView(block, 0, contentLength! - offset);
      }
    }

    return http.ByteStream(chunks());
  }
}
