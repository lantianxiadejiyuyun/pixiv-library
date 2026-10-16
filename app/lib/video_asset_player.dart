import 'dart:async';
import 'package:flutter/material.dart';
import 'package:video_player/video_player.dart';
import 'api.dart';

/// The reader awaits this before saving a position or changing the active asset.
class VideoAssetPlayback {
  Object? _owner;
  Future<void> Function()? _pause;
  Future<void> pause() async => await _pause?.call();
}

class VideoAssetPlayer extends StatefulWidget {
  final LibraryApi api;
  final String assetId;
  final bool active;
  final double initialOffset;
  final VideoAssetPlayback? playback;
  final void Function(double offset, bool immediate)? onPosition;
  const VideoAssetPlayer({
    super.key,
    required this.api,
    required this.assetId,
    required this.active,
    this.initialOffset = 0,
    this.playback,
    this.onPosition,
  });

  @override
  State<VideoAssetPlayer> createState() => _VideoAssetPlayerState();
}

class _VideoAssetPlayerState extends State<VideoAssetPlayer>
    with WidgetsBindingObserver {
  VideoPlayerController? _controller;
  Completer<void>? _cancelInitialization;
  DialogRoute<void>? _fullscreenRoute;
  bool _ready = false, _failed = false, _fullscreen = false;
  int _generation = 0;
  late double _resumeOffset;
  Duration _lastPublished = const Duration(seconds: -1);

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _resumeOffset = _ratio(widget.initialOffset);
    if (widget.active) unawaited(_load());
  }

  @override
  void didUpdateWidget(VideoAssetPlayer oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.assetId != widget.assetId || oldWidget.api != widget.api) {
      _release();
      _resumeOffset = _ratio(widget.initialOffset);
      if (widget.active) unawaited(_load());
    } else if (oldWidget.active != widget.active) {
      if (widget.active) {
        unawaited(_load());
      } else {
        _release();
      }
    }
    if (oldWidget.playback != widget.playback) {
      _detach(oldWidget.playback);
      if (widget.active) _attach();
    }
  }

  double _ratio(double value) => value.isFinite ? value.clamp(0, 1) : 0;

  void _attach() {
    widget.playback?._owner = this;
    widget.playback?._pause = _pause;
  }

  void _detach(VideoAssetPlayback? playback) {
    if (playback?._owner == this) {
      playback?._owner = null;
      playback?._pause = null;
    }
  }

  Future<void> _load() async {
    final generation = ++_generation;
    final cancellation = Completer<void>();
    _cancelInitialization = cancellation;
    _failed = false;
    _ready = false;
    _lastPublished = const Duration(seconds: -1);
    _attach();
    VideoPlayerController? controller;
    try {
      final headers = widget.api.mediaHeaders();
      final url = widget.api.resolve('/assets/${widget.assetId}/content');
      controller = VideoPlayerController.networkUrl(url, httpHeaders: headers);
      _controller = controller;
      controller.addListener(_listen);
      await Future.any([
        controller.initialize(),
        cancellation.future,
      ]).timeout(const Duration(seconds: 30));
      if (!mounted || generation != _generation) return;
      _cancelInitialization = null;
      final duration = controller.value.duration;
      if (duration > Duration.zero && _resumeOffset > 0) {
        await controller.seekTo(
          Duration(
            milliseconds: (duration.inMilliseconds * _resumeOffset).round(),
          ),
        );
      }
      if (!mounted || generation != _generation) return;
      setState(() => _ready = true);
      _publish(controller.value.position, false);
    } catch (_) {
      if (!mounted || generation != _generation) return;
      // Native errors can contain a full authenticated URL or transport details.
      _release();
      setState(() => _failed = true);
    }
  }

  void _listen() {
    final controller = _controller;
    if (!mounted || controller == null || !_ready || !widget.active) return;
    if (controller.value.hasError) {
      _release();
      setState(() => _failed = true);
      return;
    }
    final position = controller.value.position;
    if ((position - _lastPublished).abs() >=
        const Duration(milliseconds: 250)) {
      _publish(position, !controller.value.isPlaying);
    }
  }

  void _publish(Duration position, bool immediate) {
    final controller = _controller;
    if (!mounted || controller == null || !_ready || !widget.active) return;
    final length = controller.value.duration.inMilliseconds;
    if (length <= 0) return;
    _lastPublished = position;
    _resumeOffset = _ratio(position.inMilliseconds / length);
    widget.onPosition?.call(_resumeOffset, immediate);
  }

  Future<void> _pause() async {
    final controller = _controller;
    if (controller == null || !_ready) return;
    try {
      await controller.pause();
      final position = await controller.position.timeout(
        const Duration(seconds: 2),
      );
      if (mounted && controller == _controller) {
        _publish(position ?? controller.value.position, true);
      }
    } catch (_) {
      if (mounted && controller == _controller) {
        _publish(controller.value.position, true);
      }
    }
  }

  Future<void> _toggle() async {
    final controller = _controller;
    if (controller == null || !_ready || !widget.active) return;
    if (controller.value.isPlaying) {
      await _pause();
    } else {
      try {
        if (controller.value.position >= controller.value.duration) {
          await controller.seekTo(Duration.zero);
        }
        await controller.play();
      } catch (_) {
        if (mounted) {
          _release();
          setState(() => _failed = true);
        }
      }
    }
  }

  Future<void> _seek(double offset) async {
    final controller = _controller;
    if (controller == null || !_ready) return;
    try {
      await controller.seekTo(
        Duration(
          milliseconds: (controller.value.duration.inMilliseconds * offset)
              .round(),
        ),
      );
      if (mounted && controller == _controller) {
        _publish(controller.value.position, true);
      }
    } catch (_) {
      if (mounted) {
        _release();
        setState(() => _failed = true);
      }
    }
  }

  Future<void> _openFullscreen() async {
    final controller = _controller;
    if (controller == null || !_ready || _fullscreen) return;
    final navigator = Navigator.of(context);
    setState(() => _fullscreen = true);
    final route = DialogRoute<void>(
      context: context,
      builder: (context) => Dialog.fullscreen(
        backgroundColor: Colors.black,
        child: SafeArea(
          child: Column(
            children: [
              Align(
                alignment: Alignment.centerRight,
                child: IconButton(
                  tooltip: '退出全屏',
                  color: Colors.white,
                  onPressed: () => Navigator.pop(context),
                  icon: const Icon(Icons.fullscreen_exit),
                ),
              ),
              Expanded(
                child: _VideoSurface(
                  controller: controller,
                  toggle: _toggle,
                  seek: _seek,
                ),
              ),
            ],
          ),
        ),
      ),
    );
    _fullscreenRoute = route;
    await navigator.push(route);
    if (_fullscreenRoute == route) _fullscreenRoute = null;
    if (mounted) setState(() => _fullscreen = false);
  }

  void _release() {
    _generation++;
    final cancellation = _cancelInitialization;
    _cancelInitialization = null;
    if (cancellation != null && !cancellation.isCompleted) {
      cancellation.complete();
    }
    final controller = _controller;
    _controller = null;
    _ready = false;
    _detach(widget.playback);
    if (controller != null) {
      controller.removeListener(_listen);
      unawaited(_destroy(controller));
    }
    final route = _fullscreenRoute;
    _fullscreenRoute = null;
    if (route != null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (route.isActive) route.navigator?.removeRoute(route);
      });
    }
  }

  Future<void> _destroy(VideoPlayerController controller) async {
    try {
      await controller.pause();
    } catch (_) {
      /* Already released. */
    }
    try {
      await controller.dispose();
    } catch (_) {
      /* Native teardown failed. */
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed) unawaited(_pause());
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _release();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (!widget.active) {
      return const Center(child: Icon(Icons.play_circle_outline, size: 48));
    }
    if (_failed) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Text('视频暂时无法播放，请检查网络或视频格式。', textAlign: TextAlign.center),
              const SizedBox(height: 12),
              FilledButton.icon(
                onPressed: () {
                  setState(() => _failed = false);
                  unawaited(_load());
                },
                icon: const Icon(Icons.refresh),
                label: const Text('重试视频'),
              ),
            ],
          ),
        ),
      );
    }
    if (!_ready) return const Center(child: CircularProgressIndicator());
    if (_fullscreen) return const Center(child: Text('视频正在全屏播放'));
    return _VideoSurface(
      controller: _controller!,
      toggle: _toggle,
      seek: _seek,
      fullscreen: _openFullscreen,
    );
  }
}

String _time(Duration duration) {
  final seconds = duration.inSeconds;
  final minutes = (seconds ~/ 60).toString().padLeft(2, '0');
  return '$minutes:${(seconds % 60).toString().padLeft(2, '0')}';
}

class _VideoSurface extends StatelessWidget {
  final VideoPlayerController controller;
  final Future<void> Function() toggle;
  final Future<void> Function(double) seek;
  final Future<void> Function()? fullscreen;
  const _VideoSurface({
    required this.controller,
    required this.toggle,
    required this.seek,
    this.fullscreen,
  });

  @override
  Widget build(BuildContext context) => ColoredBox(
    color: Colors.black,
    child: ValueListenableBuilder<VideoPlayerValue>(
      valueListenable: controller,
      builder: (context, value, _) => Column(
        children: [
          Expanded(
            child: Center(
              child: AspectRatio(
                aspectRatio: value.aspectRatio > 0 ? value.aspectRatio : 16 / 9,
                child: Stack(
                  alignment: Alignment.center,
                  children: [
                    VideoPlayer(controller),
                    if (value.isBuffering) const CircularProgressIndicator(),
                  ],
                ),
              ),
            ),
          ),
          Row(
            children: [
              IconButton(
                tooltip: value.isPlaying ? '暂停视频' : '播放视频',
                color: Colors.white,
                onPressed: toggle,
                icon: Icon(value.isPlaying ? Icons.pause : Icons.play_arrow),
              ),
              Text(
                _time(value.position),
                style: const TextStyle(color: Colors.white),
              ),
              Expanded(
                child: Slider(
                  semanticFormatterCallback: (v) => '${(v * 100).round()}%',
                  value: value.duration.inMilliseconds > 0
                      ? (value.position.inMilliseconds /
                                value.duration.inMilliseconds)
                            .clamp(0, 1)
                      : 0,
                  onChanged: value.duration > Duration.zero ? seek : null,
                ),
              ),
              Text(
                _time(value.duration),
                style: const TextStyle(color: Colors.white),
              ),
              if (fullscreen != null)
                IconButton(
                  tooltip: '全屏播放',
                  color: Colors.white,
                  onPressed: fullscreen,
                  icon: const Icon(Icons.fullscreen),
                ),
              const SizedBox(width: 8),
            ],
          ),
        ],
      ),
    ),
  );
}
