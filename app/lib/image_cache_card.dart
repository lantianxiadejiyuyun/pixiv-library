import 'package:flutter/material.dart';
import 'api.dart';
import 'app_image_cache.dart';

class ImageCacheCard extends StatefulWidget {
  final LibraryApi api;
  const ImageCacheCard({super.key, required this.api});

  @override
  State<ImageCacheCard> createState() => _ImageCacheCardState();
}

class _ImageCacheCardState extends State<ImageCacheCard> {
  ImageCacheStats? _stats;
  bool _busy = true;
  String? _notice;
  int _request = 0;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  @override
  void didUpdateWidget(covariant ImageCacheCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.api != oldWidget.api) {
      _stats = null;
      _refresh();
    }
  }

  Future<void> _refresh({bool clear = false}) async {
    final request = ++_request;
    setState(() {
      _busy = true;
      _notice = null;
    });
    try {
      if (clear) {
        await widget.api.clearImageCache();
        PaintingBinding.instance.imageCache.clear();
        PaintingBinding.instance.imageCache.clearLiveImages();
      }
      final stats = await widget.api.imageCacheStats();
      if (!mounted || request != _request) return;
      setState(() {
        _stats = stats;
        if (clear) _notice = '图片缓存已清除';
      });
    } catch (_) {
      if (mounted && request == _request) {
        setState(() => _notice = clear ? '图片缓存清理失败，请重试' : '无法读取图片缓存，请重试');
      }
    } finally {
      if (mounted && request == _request) setState(() => _busy = false);
    }
  }

  String _size(int bytes) => bytes >= 1024 * 1024 * 1024
      ? '${(bytes / (1024 * 1024 * 1024)).toStringAsFixed(1)} GB'
      : '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';

  @override
  Widget build(BuildContext context) => Card(
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('图片缓存', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 8),
          if (_stats != null) ...[
            Text('${_size(_stats!.bytes)} · ${_stats!.entries} 张图片'),
            Text('上限 ${_size(_stats!.maxBytes)}，较早使用的图片自动清理。'),
          ],
          if (_busy)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 12),
              child: LinearProgressIndicator(),
            ),
          if (_notice != null)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child: Text(_notice!),
            ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            children: [
              OutlinedButton.icon(
                onPressed: _busy ? null : () => _refresh(clear: true),
                icon: const Icon(Icons.cleaning_services_outlined),
                label: const Text('清除图片缓存'),
              ),
              TextButton(
                onPressed: _busy ? null : _refresh,
                child: const Text('刷新用量'),
              ),
            ],
          ),
        ],
      ),
    ),
  );
}
