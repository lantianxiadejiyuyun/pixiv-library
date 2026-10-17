import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart' show ScrollCacheExtent;
import 'api.dart';
import 'api_image.dart';
import 'models.dart';

class ComicFlowController {
  void Function(int)? _jump;
  void jumpTo(int page) => _jump?.call(page);
}

/// SliverList otherwise extrapolates every remaining page from the currently
/// mounted pages' average height. With mixed portrait/landscape pages that
/// changes the bottom on reverse scrolling, even after every page was read.
class _PageExtentDelegate extends SliverChildBuilderDelegate {
  final List<double> remainingExtents;
  _PageExtentDelegate(super.builder, {required this.remainingExtents})
    : super(childCount: remainingExtents.length - 1);

  @override
  double estimateMaxScrollOffset(
    int firstIndex,
    int lastIndex,
    double leadingScrollOffset,
    double trailingScrollOffset,
  ) => trailingScrollOffset + remainingExtents[lastIndex + 1];
}

class _PageScrollController extends ScrollController {
  _PageScrollController() : super(keepScrollOffset: false);

  // Geometry can change again before the previous layout correction is applied.
  double get geometryPixels =>
      offset + ((position as _PageScrollPosition).correction ?? 0);

  void correctGeometry(double delta, {bool restore = false}) {
    if (!hasClients) return;
    final current = position as _PageScrollPosition;
    current.correction = (current.correction ?? 0) + delta;
    current.restore = current.restore || restore;
  }

  @override
  ScrollPosition createScrollPosition(
    ScrollPhysics physics,
    ScrollContext context,
    ScrollPosition? oldPosition,
  ) => _PageScrollPosition(
    physics: physics,
    context: context,
    oldPosition: oldPosition,
  );
}

class _PageScrollPosition extends ScrollPositionWithSingleContext {
  double? correction;
  bool restore = false;
  _PageScrollPosition({
    required super.physics,
    required super.context,
    ScrollPosition? oldPosition,
  }) : super(
         initialPixels: 0,
         keepScrollOffset: false,
         oldPosition: oldPosition,
       ) {
    if (oldPosition is _PageScrollPosition) {
      correction = oldPosition.correction;
      restore = oldPosition.restore;
    }
  }

  @override
  bool applyContentDimensions(double minScrollExtent, double maxScrollExtent) {
    // A replacement position absorbs pixels/activity but not haveDimensions.
    if (correction != null && !haveDimensions) {
      final previous = pixels;
      _correctGeometry(minScrollExtent, maxScrollExtent);
      final accepted = super.applyContentDimensions(
        minScrollExtent,
        maxScrollExtent,
      );
      return accepted && (pixels - previous).abs() < .0001;
    }
    // Even two size changes whose total cancels out need an anchor correction.
    if (correction != null) correctBy(0);
    return super.applyContentDimensions(minScrollExtent, maxScrollExtent);
  }

  @override
  bool correctForNewDimensions(
    ScrollMetrics oldPosition,
    ScrollMetrics newPosition,
  ) {
    if (correction == null) {
      return super.correctForNewDimensions(oldPosition, newPosition);
    }
    return _correctGeometry(
      newPosition.minScrollExtent,
      newPosition.maxScrollExtent,
    );
  }

  bool _correctGeometry(double minimum, double maximum) {
    final delta = correction!;
    correction = null;
    final target = restore
        ? (pixels + delta).clamp(minimum, maximum)
        : pixels + delta;
    restore = false;
    if ((target - pixels).abs() < .0001) return true;
    // Layout correction keeps DragScrollActivity alive. jumpTo would cancel
    // the finger's gesture. If an unknown tail shrinks, normal physics settles
    // any new overscroll on release instead of snapping during the drag.
    correctPixels(target);
    return false;
  }
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
  late _PageScrollController _scroll;
  late int _anchor;
  int _version = 0;
  double? _pendingRestore;
  final _viewport = GlobalKey();
  final _center = UniqueKey();
  final _keys = <int, GlobalKey>{};
  // Keep geometry, not decoded images, when lazy slivers recycle their children.
  // A cache/disk read is still asynchronous: its placeholder must occupy the
  // same height as the page that was previously laid out.
  final _aspectRatios = <String, double>{};
  final _pendingRatios = <String, double>{};
  double _layoutWidth = 0;
  bool _sizesQueued = false;
  bool _queued = false;
  @override
  void initState() {
    super.initState();
    _anchor = widget.initialPage.clamp(0, widget.assets.length - 1);
    _pendingRestore = widget.initialOffset > 0
        ? widget.initialOffset.clamp(0, 1)
        : null;
    _scroll = _PageScrollController()..addListener(_queueTrack);
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
      _scroll = _PageScrollController()..addListener(_queueTrack);
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
    _keys.removeWhere((_, key) => key.currentContext == null);
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

  double _pageTop(int page) {
    var top = 0.0;
    for (
      var p = page < _anchor ? page : _anchor;
      p < (page < _anchor ? _anchor : page);
      p++
    ) {
      top += _layoutWidth / (_aspectRatios[widget.assets[p].id] ?? (2 / 3)) + 4;
    }
    return page < _anchor ? -top : top;
  }

  int _visibleAnchor() {
    final viewport = _viewport.currentContext?.findRenderObject();
    if (viewport is! RenderBox || !viewport.hasSize) return _anchor;
    int? loaded, fallback;
    var loadedTop = double.infinity, fallbackTop = double.infinity;
    for (final page in _keys.keys) {
      final rect = _rect(page);
      if (rect == null ||
          rect.bottom <= 0 ||
          rect.top >= viewport.size.height) {
        continue;
      }
      if (rect.top < fallbackTop) {
        fallback = page;
        fallbackTop = rect.top;
      }
      if (_aspectRatios.containsKey(widget.assets[page].id) &&
          rect.top < loadedTop) {
        loaded = page;
        loadedTop = rect.top;
      }
    }
    return loaded ?? fallback ?? _anchor;
  }

  void _imageSize(int page, double ratio) {
    if (!mounted || !ratio.isFinite || ratio <= 0) return;
    final id = widget.assets[page].id;
    if (_aspectRatios[id] == ratio) return;
    _pendingRatios[id] = ratio;
    if (_sizesQueued) return;
    _sizesQueued = true;
    scheduleMicrotask(() {
      _sizesQueued = false;
      if (!mounted) return;
      final anchor = _visibleAnchor();
      final previousTop = _pageTop(anchor);
      setState(() => _aspectRatios.addAll(_pendingRatios));
      _pendingRatios.clear();
      var delta = _pageTop(anchor) - previousTop;
      var restoring = false;
      if (_pendingRestore != null &&
          _aspectRatios.containsKey(widget.assets[_anchor].id) &&
          _scroll.hasClients) {
        delta =
            (_layoutWidth / _aspectRatios[widget.assets[_anchor].id]! + 4) *
                _pendingRestore! -
            _scroll.geometryPixels;
        _pendingRestore = null;
        restoring = true;
      }
      _scroll.correctGeometry(delta, restore: restoring);
      _queueTrack();
    });
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
            child: LayoutBuilder(
              builder: (_, constraints) {
                final ratio = _aspectRatios[asset.id];
                return SizedBox(
                  width: double.infinity,
                  height: constraints.maxWidth / (ratio ?? (2 / 3)),
                  child: ApiImage(
                    key: ValueKey(asset.id),
                    api: widget.api,
                    path: '/assets/${asset.id}/content',
                    naturalHeight: true,
                    fit: BoxFit.fitWidth,
                    onAspectRatio: (ratio) => _imageSize(page, ratio),
                  ),
                );
              },
            ),
          ),
        ),
      ),
    );
  }

  List<double> _remainingExtents(int start, int count, int step, double width) {
    final extents = List<double>.filled(count + 1, 0);
    for (var index = count - 1; index >= 0; index--) {
      final asset = widget.assets[start + index * step];
      extents[index] =
          extents[index + 1] + width / (_aspectRatios[asset.id] ?? (2 / 3)) + 4;
    }
    return extents;
  }

  Widget _sliver(int start, int count, int step, double width, {Key? key}) {
    final remaining = _remainingExtents(start, count, step, width);
    return SliverVariedExtentList(
      key: key,
      itemExtentBuilder: (index, _) =>
          index < count ? remaining[index] - remaining[index + 1] : null,
      delegate: _PageExtentDelegate(
        (_, index) => _image(start + index * step),
        remainingExtents: remaining,
      ),
    );
  }

  void _resize(double width) {
    if (_layoutWidth == width) return;
    if (_layoutWidth > 0 &&
        _scroll.hasClients &&
        _scroll.position.hasContentDimensions) {
      final page = _visibleAnchor();
      final ratio = _aspectRatios[widget.assets[page].id] ?? (2 / 3);
      final pixels = _scroll.geometryPixels;
      final fraction = (pixels - _pageTop(page)) / (_layoutWidth / ratio + 4);
      _layoutWidth = width;
      _scroll.correctGeometry(
        _pageTop(page) + fraction * (width / ratio + 4) - pixels,
        restore: true,
      );
      _queueTrack();
    } else {
      _layoutWidth = width;
    }
  }

  @override
  Widget build(BuildContext context) => SizedBox.expand(
    key: _viewport,
    child: LayoutBuilder(
      builder: (context, constraints) {
        _resize(constraints.maxWidth);
        return NotificationListener<SizeChangedLayoutNotification>(
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
                _sliver(_anchor - 1, _anchor, -1, constraints.maxWidth),
                _sliver(
                  _anchor,
                  widget.assets.length - _anchor,
                  1,
                  constraints.maxWidth,
                  key: _center,
                ),
              ],
            ),
          ),
        );
      },
    ),
  );
}
