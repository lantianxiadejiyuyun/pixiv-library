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
  const ApiImage({
    super.key,
    required this.api,
    required this.path,
    this.fit = BoxFit.cover,
    this.naturalHeight = false,
    this.onReady,
  });
  @override
  State<ApiImage> createState() => _ApiImageState();
}

class _ApiImageState extends State<ApiImage> {
  late Future<Uint8List> _bytes;
  bool _reportedReady = false;
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
      _reportedReady = false;
      _bytes = widget.api.image(widget.path);
    }
  }

  @override
  Widget build(BuildContext context) => FutureBuilder<Uint8List>(
    future: _bytes,
    builder: (context, snapshot) {
      if (snapshot.hasError) {
        final retry = Center(
          child: IconButton(
            tooltip: '重新加载图片',
            onPressed: () => setState(() {
              _bytes = widget.api.image(widget.path);
            }),
            icon: const Icon(Icons.refresh_rounded),
          ),
        );
        return widget.naturalHeight
            ? AspectRatio(aspectRatio: 2 / 3, child: retry)
            : retry;
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
      return Image.memory(
        snapshot.data!,
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
        cacheWidth:
            (MediaQuery.sizeOf(context).width *
                    MediaQuery.devicePixelRatioOf(context))
                .ceil()
                .clamp(480, 2048),
        errorBuilder: (_, _, _) => widget.naturalHeight
            ? const AspectRatio(
                aspectRatio: 2 / 3,
                child: Center(child: Icon(Icons.broken_image_outlined)),
              )
            : const Center(child: Icon(Icons.broken_image_outlined)),
      );
    },
  );
}
