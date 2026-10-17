import 'package:flutter/material.dart';
import 'api.dart';

class AuthorDownloadControls extends StatefulWidget {
  final LibraryApi api;
  final String authorId;
  final Map<String, dynamic> initial;
  const AuthorDownloadControls({
    super.key,
    required this.api,
    required this.authorId,
    required this.initial,
  });
  @override
  State<AuthorDownloadControls> createState() => _AuthorDownloadControlsState();
}

class _AuthorDownloadControlsState extends State<AuthorDownloadControls> {
  late Map<String, dynamic> _settings;
  bool _busy = false;
  String? _error;
  @override
  void initState() {
    super.initState();
    _settings = Map.of(widget.initial);
  }

  @override
  void didUpdateWidget(covariant AuthorDownloadControls oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.authorId != widget.authorId) {
      _settings = Map.of(widget.initial);
    }
  }

  Future<void> _save(String key, bool value) async {
    final ident = widget.authorId;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final result =
          await widget.api.put('/pixiv/authors/$ident/download-settings', {
                'download_works': key == 'download_works'
                    ? value
                    : _settings['download_works'] == true,
                'download_bookmarks': key == 'download_bookmarks'
                    ? value
                    : _settings['download_bookmarks'] == true,
              })
              as Map<String, dynamic>;
      if (mounted && widget.authorId == ident) {
        setState(() => _settings = result);
      }
    } catch (error) {
      if (mounted && widget.authorId == ident) {
        setState(
          () => _error = error is ApiFailure ? error.message : '保存失败，请重试',
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Wrap(
        spacing: 12,
        children: [
          FilterChip(
            label: const Text('下载作者作品'),
            selected: _settings['download_works'] == true,
            onSelected: _busy
                ? null
                : (value) => _save('download_works', value),
          ),
          FilterChip(
            label: const Text('下载作者公开收藏'),
            selected: _settings['download_bookmarks'] == true,
            onSelected: _busy
                ? null
                : (value) => _save('download_bookmarks', value),
          ),
        ],
      ),
      if (_error != null)
        Text(
          _error!,
          style: TextStyle(color: Theme.of(context).colorScheme.error),
        ),
    ],
  );
}
