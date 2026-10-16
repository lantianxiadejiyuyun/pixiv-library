import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'article_body.dart';

typedef _LayoutKey = ({
  String text,
  String title,
  double width,
  TextStyle style,
  TextStyle titleStyle,
  TextScaler scaler,
  TextDirection direction,
  Locale? locale,
  TextHeightBehavior? heightBehavior,
  double footerHeight,
});

/// Uses measured paragraph heights, not an estimated scroll extent. The saved
/// position remains the fraction of the complete chapter's scrollable distance.
/// Only paragraphs near the viewport have widgets/render objects at any time.
class NovelReaderBody extends StatefulWidget {
  final String text, title;
  final double fontSize, restoreOffset;
  final int restoreRevision;
  final ScrollController controller;
  final Widget footer;
  final VoidCallback onLayoutStart, onLayoutReady;

  const NovelReaderBody({
    super.key,
    required this.text,
    required this.title,
    required this.fontSize,
    required this.restoreOffset,
    this.restoreRevision = 0,
    required this.controller,
    required this.footer,
    required this.onLayoutStart,
    required this.onLayoutReady,
  });

  @override
  State<NovelReaderBody> createState() => _NovelReaderBodyState();
}

class _NovelReaderBodyState extends State<NovelReaderBody> {
  final _layouts = <_LayoutKey, _ArticleLayout>{};
  final _paragraphs = <String, List<String>>{};
  _LayoutKey? _key;
  _ArticleLayout? _layout;
  int _version = 0;
  int _restoreRevision = -1;
  double _viewport = 0;
  (int, int)? _range;

  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_scroll);
    PaintingBinding.instance.systemFonts.addListener(_fontsChanged);
  }

  @override
  void didUpdateWidget(NovelReaderBody oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.controller != widget.controller) {
      oldWidget.controller.removeListener(_scroll);
      widget.controller.addListener(_scroll);
    }
  }

  @override
  void dispose() {
    _version++;
    widget.controller.removeListener(_scroll);
    PaintingBinding.instance.systemFonts.removeListener(_fontsChanged);
    super.dispose();
  }

  void _fontsChanged() {
    _layouts.clear();
    setState(() => _key = null);
  }

  void _scroll() {
    final layout = _layout;
    if (layout == null || !widget.controller.hasClients) return;
    final range = layout.visible(widget.controller.offset, _viewport);
    if (range != _range) setState(() => _range = range);
  }

  Future<List<String>> _parse(String text) async {
    final cached = _paragraphs.remove(text);
    if (cached != null) {
      _paragraphs[text] = cached;
      return cached;
    }
    // Large normalization/split work runs outside the UI isolate on Android/iOS.
    final result = text.length >= 256 * 1024
        ? await compute(parseArticleParagraphs, text)
        : parseArticleParagraphs(text);
    if (!mounted) return result;
    _paragraphs[text] = result;
    while (_paragraphs.length > 3) {
      _paragraphs.remove(_paragraphs.keys.first);
    }
    return result;
  }

  Future<void> _prepare(_LayoutKey key, int version) async {
    // Even a cached layout is installed after build; cache hits must not call
    // setState synchronously from LayoutBuilder.
    await Future<void>.value();
    if (!mounted || version != _version) return;
    var layout = _layouts.remove(key);
    if (layout == null) {
      final sourceParagraphs = await _parse(key.text);
      if (!mounted || version != _version) return;
      final painter = TextPainter(
        textDirection: key.direction,
        textScaler: key.scaler,
        locale: key.locale,
        textHeightBehavior: key.heightBehavior,
      );
      try {
        painter.text = TextSpan(text: key.title, style: key.titleStyle);
        painter.layout(maxWidth: key.width);
        final titleHeight = painter.height;
        final tops = <double>[];
        final heights = <double>[];
        final paragraphs = <String>[];
        var top = 12 + titleHeight + 22;
        final gap = key.scaler.scale(key.style.fontSize!) * .9;
        final slice = Stopwatch()..start();
        for (var i = 0; i < sourceParagraphs.length; i++) {
          final source = sourceParagraphs[i];
          // Long LTR paragraphs are split only on already laid-out soft line
          // boundaries, with no added gap. Preserve full shaping context for
          // bidi/control text and special first/last-line height behavior.
          final canChunk =
              key.direction == TextDirection.ltr &&
              !RegExp(
                r'[\n\u0590-\u08ff\u200e-\u200f\u202a-\u202e\u2066-\u2069]',
              ).hasMatch(source) &&
              (key.heightBehavior?.applyHeightToFirstAscent ?? true) &&
              (key.heightBehavior?.applyHeightToLastDescent ?? true);
          var consumed = 0;
          while (consumed < source.length) {
            final text = _measurePiece(
              source,
              consumed,
              canChunk,
              painter,
              key,
            );
            consumed += text.length;
            paragraphs.add(text);
            tops.add(top);
            heights.add(painter.height);
            top += painter.height;
            // Yield during long layouts; only the visible blocks will need
            // widgets/render objects. A newer chapter cancels this work.
            if (paragraphs.length % 64 == 0 || slice.elapsedMilliseconds >= 4) {
              await SchedulerBinding.instance.endOfFrame;
              if (!mounted || version != _version) return;
              slice.reset();
            }
          }
          if (i != sourceParagraphs.length - 1) top += gap;
        }
        layout = _ArticleLayout(
          paragraphs: paragraphs,
          tops: tops,
          heights: heights,
          titleHeight: titleHeight,
          footerTop: top + 28,
          totalHeight: top + 28 + key.footerHeight + 36,
        );
      } finally {
        painter.dispose();
      }
    }
    if (!mounted || version != _version) return;
    _layouts[key] = layout;
    while (_layouts.length > 3) {
      _layouts.remove(_layouts.keys.first);
    }
    setState(() {
      _layout = layout;
      _range = null;
    });
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || version != _version || !widget.controller.hasClients) {
        return;
      }
      final ratio = widget.restoreOffset.isFinite
          ? widget.restoreOffset.clamp(0.0, 1.0)
          : 0.0;
      widget.controller.jumpTo(
        widget.controller.position.maxScrollExtent * ratio,
      );
      widget.onLayoutReady();
    });
  }

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      final defaultStyle = DefaultTextStyle.of(context).style;
      final bold = MediaQuery.boldTextOf(context);
      final scaler = MediaQuery.textScalerOf(context);
      final _LayoutKey key = (
        text: widget.text,
        title: widget.title,
        width: math.min(760, math.max(1, constraints.maxWidth - 48)),
        style: defaultStyle.merge(
          TextStyle(
            fontSize: widget.fontSize,
            height: 1.85,
            fontWeight: bold ? FontWeight.bold : null,
          ),
        ),
        titleStyle: defaultStyle.merge(
          Theme.of(context).textTheme.headlineSmall?.copyWith(
            height: 1.5,
            fontWeight: bold ? FontWeight.bold : null,
          ),
        ),
        scaler: scaler,
        direction: Directionality.of(context),
        locale: Localizations.maybeLocaleOf(context),
        heightBehavior: DefaultTextHeightBehavior.maybeOf(context),
        footerHeight: math.max(48, scaler.scale(16) * 1.5 + 16),
      );
      final resized = _viewport != constraints.maxHeight;
      _viewport = constraints.maxHeight;
      if (_key != key ||
          _restoreRevision != widget.restoreRevision ||
          resized) {
        _key = key;
        _restoreRevision = widget.restoreRevision;
        _layout = null;
        _range = null;
        widget.onLayoutStart();
        unawaited(_prepare(key, ++_version));
      }
      final layout = _layout;
      if (layout == null) {
        return const Center(child: CircularProgressIndicator());
      }
      final offset = widget.controller.hasClients
          ? widget.controller.offset
          : 0.0;
      final range = _range = layout.visible(offset, _viewport);
      return SingleChildScrollView(
        controller: widget.controller,
        child: Center(
          child: SizedBox(
            width: key.width,
            height: layout.totalHeight,
            child: Stack(
              children: [
                Positioned(
                  top: 12,
                  left: 0,
                  right: 0,
                  height: layout.titleHeight,
                  child: _text(key.title, key.titleStyle, key),
                ),
                for (var i = range.$1; i < range.$2; i++)
                  Positioned(
                    key: ValueKey('novel-paragraph-$i'),
                    top: layout.tops[i],
                    left: 0,
                    right: 0,
                    height: layout.heights[i],
                    child: RepaintBoundary(
                      child: _text(layout.paragraphs[i], key.style, key),
                    ),
                  ),
                Positioned(
                  top: layout.footerTop,
                  left: 0,
                  right: 0,
                  height: key.footerHeight,
                  child: widget.footer,
                ),
              ],
            ),
          ),
        ),
      );
    },
  );

  Widget _text(String text, TextStyle style, _LayoutKey key) => Text(
    text,
    style: style,
    textScaler: key.scaler,
    textDirection: key.direction,
    locale: key.locale,
    textHeightBehavior: key.heightBehavior,
    textWidthBasis: TextWidthBasis.parent,
  );

  String _measurePiece(
    String source,
    int start,
    bool canChunk,
    TextPainter painter,
    _LayoutKey key,
  ) {
    var end = canChunk ? math.min(start + 2048, source.length) : source.length;
    // Never cut the probe in the middle of a UTF-16 surrogate pair.
    if (end < source.length &&
        source.codeUnitAt(end - 1) >= 0xd800 &&
        source.codeUnitAt(end - 1) <= 0xdbff) {
      end--;
    }
    var text = source.substring(start, end);
    painter.text = TextSpan(text: text, style: key.style);
    painter.layout(maxWidth: key.width);
    if (end < source.length) {
      final lines = painter.computeLineMetrics();
      if (lines.length > 1) {
        // The probe's last line may change when more source text follows it.
        final complete = lines[lines.length - 2];
        final position = painter.getPositionForOffset(
          Offset(key.width / 2, complete.baseline - complete.ascent / 2),
        );
        final boundary = painter.getLineBoundary(position).end;
        if (boundary > 0) {
          text = text.substring(0, boundary);
          painter.text = TextSpan(text: text, style: key.style);
          painter.layout(maxWidth: key.width);
          return text;
        }
      }
      // Zero-width/control-only probes may have no safe visual line boundary.
      // Keep their original shaping instead of inventing extra lines.
      text = source.substring(start);
      painter.text = TextSpan(text: text, style: key.style);
      painter.layout(maxWidth: key.width);
    }
    return text;
  }
}

class _ArticleLayout {
  final List<String> paragraphs;
  final List<double> tops, heights;
  final double titleHeight, footerTop, totalHeight;

  _ArticleLayout({
    required this.paragraphs,
    required this.tops,
    required this.heights,
    required this.titleHeight,
    required this.footerTop,
    required this.totalHeight,
  });

  (int, int) visible(double offset, double viewport) {
    const overscan = 320.0;
    var low = 0, high = tops.length;
    while (low < high) {
      final mid = (low + high) ~/ 2;
      if (tops[mid] + heights[mid] < offset - overscan) {
        low = mid + 1;
      } else {
        high = mid;
      }
    }
    var end = low;
    while (end < tops.length && tops[end] <= offset + viewport + overscan) {
      end++;
    }
    return (low, end);
  }
}
