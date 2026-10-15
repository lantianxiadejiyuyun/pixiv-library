import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart' show ScrollCacheExtent;
import 'api.dart';
import 'api_image.dart';
import 'models.dart';

class ComicFlowController {
  void Function(int)? _jump;
  void jumpTo(int page) => _jump?.call(page);
}

/// Two lazy slivers grow around the saved page, allowing exact page jumps
/// without estimating or downloading the heights of all preceding images.
class ComicFlowReader extends StatefulWidget {
  final LibraryApi api;
  final String itemId;
  final List<LibraryAsset> assets;
  final int initialPage;
  final double initialOffset;
  final ComicFlowController controller;
  final void Function(int page, double offset) onPosition;
  const ComicFlowReader({
    super.key,
    required this.api,
    required this.itemId,
    required this.assets,
    required this.initialPage,
    this.initialOffset = 0,
    required this.controller,
    required this.onPosition,
  });
  @override
  State<ComicFlowReader> createState() => _ComicFlowReaderState();
}

class _ComicFlowReaderState extends State<ComicFlowReader> {
  late ScrollController _scroll;
  late int _anchor;
  int _version = 0;
  double? _pendingRestore;
  final _viewport = GlobalKey();
  final _center = UniqueKey();
  final _keys = <int, GlobalKey>{};
  bool _queued = false;
  @override
  void initState() {
    super.initState();
    _anchor = widget.initialPage.clamp(0, widget.assets.length - 1);
    _pendingRestore = widget.initialOffset > 0
        ? widget.initialOffset.clamp(0, 1)
        : null;
    _scroll = ScrollController(keepScrollOffset: false)
      ..addListener(_queueTrack);
    widget.controller._jump = _jump;
    _queueTrack();
  }

  @override
  void dispose() {
    if (widget.controller._jump == _jump) widget.controller._jump = null;
    _scroll.dispose();
    super.dispose();
  }

  void _jump(int page) {
    if (page < 0 || page >= widget.assets.length) return;
    final old = _scroll;
    setState(() {
      _anchor = page;
      _version++;
      _keys.clear();
      _pendingRestore = null;
      _scroll = ScrollController(keepScrollOffset: false)
        ..addListener(_queueTrack);
    });
    WidgetsBinding.instance.addPostFrameCallback((_) {
      old.dispose();
      _queueTrack();
    });
  }

  void _queueTrack() {
    if (_queued || !mounted) return;
    _queued = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _queued = false;
      if (mounted) _track();
    });
  }

  Rect? _rect(int page) {
    final viewport = _viewport.currentContext?.findRenderObject();
    final box = _keys[page]?.currentContext?.findRenderObject();
    if (viewport is! RenderBox || box is! RenderBox || !box.hasSize) {
      return null;
    }
    return box.localToGlobal(Offset.zero, ancestor: viewport) & box.size;
  }

  void _track() {
    if (_pendingRestore != null || !_scroll.hasClients) return;
    final view = _viewport.currentContext?.findRenderObject();
    if (view is! RenderBox || !view.hasSize) return;
    int? current;
    double top = double.infinity;
    for (final page in _keys.keys) {
      final rect = _rect(page);
      if (rect == null || rect.bottom <= 1 || rect.top >= view.size.height) {
        continue;
      }
      if (rect.top < top) {
        current = page;
        top = rect.top;
      }
    }
    if (current == null) return;
    final rect = _rect(current)!;
    widget.onPosition(current, (-rect.top / rect.height).clamp(0, 1));
  }

  void _ready(int page) {
    if (!mounted) return;
    if (page == _anchor && _pendingRestore != null && _scroll.hasClients) {
      final rect = _rect(page);
      if (rect != null) {
        final offset = rect.height * _pendingRestore!;
        _pendingRestore = null;
        _scroll.jumpTo(
          offset.clamp(
            _scroll.position.minScrollExtent,
            _scroll.position.maxScrollExtent,
          ),
        );
      }
    }
    _queueTrack();
  }

  void _zoom(int page) {
    final asset = widget.assets[page];
    showDialog<void>(
      context: context,
      useSafeArea: false,
      builder: (dialog) => Dialog.fullscreen(
        child: Scaffold(
          appBar: AppBar(
            title: Text('第 ${page + 1} P'),
            leading: IconButton(
              tooltip: '关闭放大',
              onPressed: () => Navigator.pop(dialog),
              icon: const Icon(Icons.close),
            ),
          ),
          body: InteractiveViewer(
            minScale: 1,
            maxScale: 5,
            child: SizedBox.expand(
              child: ApiImage(
                api: widget.api,
                path: '/assets/${asset.id}/content',
                fit: BoxFit.contain,
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _image(int page) {
    final asset = widget.assets[page];
    return Padding(
      key: _keys.putIfAbsent(page, GlobalKey.new),
      padding: const EdgeInsets.only(bottom: 4),
      child: SizeChangedLayoutNotifier(
        child: Semantics(
          label: '第 ${page + 1} P',
          hint: '打开图片缩放',
          button: true,
          onTap: () => _zoom(page),
          child: GestureDetector(
            behavior: HitTestBehavior.opaque,
            excludeFromSemantics: true,
            onDoubleTap: () => _zoom(page),
            child: ApiImage(
              key: ValueKey(asset.id),
              api: widget.api,
              path: '/assets/${asset.id}/content',
              naturalHeight: true,
              fit: BoxFit.fitWidth,
              onReady: () => _ready(page),
            ),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) => SizedBox.expand(
    key: _viewport,
    child: NotificationListener<SizeChangedLayoutNotification>(
      onNotification: (_) {
        _queueTrack();
        return false;
      },
      child: NotificationListener<UserScrollNotification>(
        onNotification: (_) {
          _pendingRestore = null;
          _queueTrack();
          return false;
        },
        child: CustomScrollView(
          key: ValueKey('${widget.itemId}:$_version'),
          controller: _scroll,
          center: _center,
          scrollCacheExtent: const ScrollCacheExtent.pixels(250),
          slivers: [
            SliverList.builder(
              itemCount: _anchor,
              itemBuilder: (_, index) => _image(_anchor - index - 1),
            ),
            SliverList.builder(
              key: _center,
              itemCount: widget.assets.length - _anchor,
              itemBuilder: (_, index) => _image(_anchor + index),
            ),
          ],
        ),
      ),
    ),
  );
}
