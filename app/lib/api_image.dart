import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'api.dart';

class ApiImage extends StatefulWidget {
  final LibraryApi api;
  final String path;
  final BoxFit fit;

  /// Fit the available width while preserving the complete page's aspect ratio.
  final bool naturalHeight;
  final VoidCallback? onReady;
  final ValueChanged<double>? onAspectRatio;
  const ApiImage({
    super.key,
    required this.api,
    required this.path,
    this.fit = BoxFit.cover,
    this.naturalHeight = false,
    this.onReady,
    this.onAspectRatio,
  });
  @override
  State<ApiImage> createState() => _ApiImageState();
}

class _ApiImageState extends State<ApiImage> {
  late Future<Uint8List> _bytes;
  bool _reportedReady = false;
  ImageStream? _sizeStream;
  ImageStreamListener? _sizeListener;

  void _clearSizeListener() {
    if (_sizeListener != null) _sizeStream?.removeListener(_sizeListener!);
    _sizeStream = null;
    _sizeListener = null;
  }

  void _observeSize(ImageProvider provider) {
    if (widget.onAspectRatio == null) return;
    final stream = provider.resolve(createLocalImageConfiguration(context));
    if (_sizeStream?.key == stream.key) return;
    _clearSizeListener();
    _sizeStream = stream;
    double? reported;
    _sizeListener = ImageStreamListener(
      (info, _) {
        final ratio = info.image.width / info.image.height;
        info.dispose();
        if (reported == ratio) return;
        reported = ratio;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted && identical(_sizeStream, stream)) {
            widget.onAspectRatio?.call(ratio);
          }
        });
      },
      onError: (Object error, StackTrace? stack) {
        // Image's errorBuilder owns the retry UI.
      },
    );
    stream.addListener(_sizeListener!);
  }

  void _retry({bool evict = false}) {
    _clearSizeListener();
    final api = widget.api;
    final path = widget.path;
    setState(() {
      _reportedReady = false;
      _bytes = (() async {
        if (evict) await api.evictImage(path);
        return api.image(path);
      })();
    });
  }

  Widget _retryButton({bool evict = false}) {
    final retry = Center(
      child: IconButton(
        tooltip: '重新加载图片',
        onPressed: () => _retry(evict: evict),
        icon: const Icon(Icons.refresh_rounded),
      ),
    );
    return widget.naturalHeight
        ? AspectRatio(aspectRatio: 2 / 3, child: retry)
        : retry;
  }

  void _ready() {
    if (_reportedReady) return;
    _reportedReady = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) widget.onReady?.call();
    });
  }

  @override
  void initState() {
    super.initState();
    _bytes = widget.api.image(widget.path);
  }

  @override
  void didUpdateWidget(covariant ApiImage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.path != widget.path || oldWidget.api != widget.api) {
      _clearSizeListener();
      _reportedReady = false;
      _bytes = widget.api.image(widget.path);
    }
  }

  @override
  void dispose() {
    _clearSizeListener();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => FutureBuilder<Uint8List>(
    key: ObjectKey(_bytes),
    future: _bytes,
    builder: (context, snapshot) {
      if (snapshot.hasError) {
        return _retryButton();
      }
      if (!snapshot.hasData) {
        const loading = Center(
          child: SizedBox(
            width: 24,
            height: 24,
            child: CircularProgressIndicator(strokeWidth: 2),
          ),
        );
        return widget.naturalHeight
            ? const AspectRatio(aspectRatio: 2 / 3, child: loading)
            : loading;
      }
      final provider = ResizeImage.resizeIfNeeded(
        (MediaQuery.sizeOf(context).width *
                MediaQuery.devicePixelRatioOf(context))
            .ceil()
            .clamp(480, 2048),
        null,
        MemoryImage(snapshot.data!),
      );
      _observeSize(provider);
      return Image(
        image: provider,
        fit: widget.fit,
        width: widget.naturalHeight ? double.infinity : null,
        frameBuilder: widget.naturalHeight
            ? (_, child, frame, wasSynchronouslyLoaded) {
                if (frame != null || wasSynchronouslyLoaded) {
                  _ready();
                  return child;
                }
                return const AspectRatio(
                  aspectRatio: 2 / 3,
                  child: Center(child: Icon(Icons.image_outlined)),
                );
              }
            : null,
        gaplessPlayback: true,
        errorBuilder: (_, _, _) => _retryButton(evict: true),
      );
    },
  );
}
