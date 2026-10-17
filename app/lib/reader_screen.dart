import 'dart:async';
import 'package:flutter/material.dart';
import 'api.dart';
import 'api_image.dart';
import 'novel_reader_body.dart';
import 'models.dart';
import 'comic_flow_reader.dart';
import 'reader_preferences.dart';
import 'video_asset_player.dart';

int _bounded(int value, int length) =>
    length <= 0 ? 0 : value.clamp(0, length - 1);
double _ratio(double value) => value.isFinite ? value.clamp(0.0, 1.0) : 0;
String _readError(Object error) =>
    error is ApiFailure ? error.message : '内容读取失败，请重试';

class ReaderScreen extends StatefulWidget {
  final LibraryApi api;
  final LibraryItem item;
  final int? initialPage;
  final ReaderPreferences preferences;
  final void Function(String itemId, ReadingPosition position)? onProgressSaved;
  const ReaderScreen({
    super.key,
    required this.api,
    required this.item,
    this.initialPage,
    this.preferences = const ReaderPreferences(),
    this.onProgressSaved,
  });
  @override
  State<ReaderScreen> createState() => _ReaderScreenState();
}

class _ReaderScreenState extends State<ReaderScreen>
    with WidgetsBindingObserver {
  late LibraryItem _item;
  late PageController _pages;
  final _textScroll = ScrollController();
  final _flowController = ComicFlowController();
  final _videoPlayback = VideoAssetPlayback();
  int _assetNavigation = 0;
  bool _flow = false, _preferredFlow = false, _modeTouched = false;
  bool _modeReady = false, _fontTouched = false;
  bool get _canFlow =>
      _item.assets.isNotEmpty &&
      _item.assets.every((asset) => asset.mimeType.startsWith('image/'));
  List<TextChapter> _chapters = [];
  int _page = 0, _chapter = 0, _loadVersion = 0;
  int _textRestoreRevision = 0;
  double _offset = 0, _fontSize = 19;
  bool _loading = false,
      _restoring = false,
      _leaving = false,
      _allowPop = false,
      _saving = false,
      _dirty = false;
  String? _error, _saveError;
  Timer? _saveTimer;
  Future<void>? _saveFuture;
  _ProgressSave? _pending;

  bool get _text =>
      _item.assets.isNotEmpty &&
      _item.assets[_page].mimeType.startsWith('text/');
  bool get _video =>
      _item.assets.isNotEmpty &&
      _item.assets[_page].mimeType.startsWith('video/');
  ReadingPosition get _position => ReadingPosition(
    page: _page,
    section: _text ? _chapter : 0,
    offset: _text || _flow || _video ? _offset : 0,
  );
  double get _percent => _item.assets.isEmpty
      ? 0
      : _text
      ? (_page +
                (_chapters.isEmpty
                    ? 0
                    : (_chapter + _offset) / _chapters.length)) /
            _item.assets.length
      : (_page + (_video ? _offset : 1)) / _item.assets.length;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _item = widget.item;
    _page = _bounded(
      widget.initialPage ?? _item.progress.page,
      _item.assets.length,
    );
    _chapter = widget.initialPage == null ? _item.progress.section : 0;
    _offset = widget.initialPage == null ? _ratio(_item.progress.offset) : 0;
    _pages = PageController(initialPage: _page);
    _textScroll.addListener(_onScroll);
    if (_item.readable && _text) _loadText(_chapter, _offset);
    if (_item.readable && !_text && !_video) _changed();
    _loadMode();
    _loadFont();
  }

  Future<void> _loadMode() async {
    bool flow = false;
    try {
      flow = await widget.preferences.flowMode().timeout(
        const Duration(milliseconds: 400),
        onTimeout: () => false,
      );
    } catch (_) {
      /* Platform storage can be unavailable. */
    }
    if (!mounted || _modeTouched) return;
    setState(() {
      _preferredFlow = flow;
      _flow = flow && _canFlow;
      _modeReady = true;
    });
  }

  Future<void> _loadFont() async {
    double size = 19;
    try {
      size = await widget.preferences.fontSize().timeout(
        const Duration(milliseconds: 400),
        onTimeout: () => 19,
      );
    } catch (_) {
      /* Reading works when platform storage is unavailable. */
    }
    if (!mounted || _fontTouched || size == _fontSize) return;
    setState(() {
      _fontSize = size.clamp(15, 30);
      if (_text) _restoring = true;
    });
  }

  void _setFlow(bool flow) {
    if ((_flow == flow && _modeReady) || !_canFlow) return;
    unawaited(_flush());
    _modeTouched = true;
    final old = _pages;
    setState(() {
      _flow = flow;
      _preferredFlow = flow;
      _modeReady = true;
      if (!flow) _pages = PageController(initialPage: _page);
    });
    if (!flow) {
      WidgetsBinding.instance.addPostFrameCallback((_) => old.dispose());
    }
    unawaited(widget.preferences.saveFlowMode(flow).catchError((_) {}));
    _changed();
  }

  void _flowPosition(int page, double offset) {
    if (!_flow ||
        _loading ||
        (_page == page && (_offset - offset).abs() < .005)) {
      return;
    }
    void updatePosition() {
      _page = page;
      _chapter = 0;
      _offset = offset;
    }

    // Fractional flow progress changes every scroll frame, but the controls
    // only change on page boundaries. Do not rebuild the entire reader for it.
    if (_page != page || !_dirty) {
      setState(updatePosition);
    } else {
      updatePosition();
    }
    _changed();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _loadVersion++;
    _saveTimer?.cancel();
    _textScroll.dispose();
    _pages.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.inactive ||
        state == AppLifecycleState.paused) {
      unawaited(_pauseVideoAndFlush());
    }
  }

  Future<void> _pauseVideoAndFlush() async {
    await _videoPlayback.pause();
    await _flush();
  }

  void _videoPosition(String itemId, int page, double offset, bool immediate) {
    if (!mounted || _item.id != itemId || _page != page || !_video) return;
    setState(() => _offset = _ratio(offset));
    _dirty = true;
    if (immediate) {
      unawaited(_flush());
    } else if (_saveTimer?.isActive != true) {
      // Continuous playback must save periodically, rather than indefinitely
      // postponing a debounce timer on every native position notification.
      _saveTimer = Timer(const Duration(seconds: 5), _flush);
    }
  }

  Future<void> _loadText(
    int section,
    double offset, {
    bool markPosition = false,
  }) async {
    final version = ++_loadVersion;
    final itemId = _item.id;
    final page = _page;
    final asset = _item.assets[_page];
    setState(() {
      _loading = true;
      _error = null;
      _restoring = true;
      _chapters = [];
    });
    try {
      final data =
          await widget.api.get('/assets/${asset.id}/text-sections')
              as Map<String, dynamic>;
      if (!mounted ||
          version != _loadVersion ||
          itemId != _item.id ||
          page != _page) {
        return;
      }
      final chapters = (data['sections'] as List)
          .map((v) => TextChapter.fromJson(v as Map<String, dynamic>))
          .toList();
      if (chapters.isEmpty) throw const ApiFailure('此文件没有可显示的正文，请在网页下载原文件');
      setState(() {
        _chapters = chapters;
        _chapter = _bounded(section, chapters.length);
        _offset = _ratio(offset);
        _textRestoreRevision++;
        _loading = false;
      });
      if (markPosition) _changed();
    } catch (e) {
      if (mounted && version == _loadVersion) {
        setState(() {
          _error = _readError(e);
          _loading = false;
          _restoring = false;
        });
      }
    }
  }

  void _onScroll() {
    if (_restoring ||
        _loading ||
        !_text ||
        !_textScroll.hasClients ||
        _chapters.isEmpty) {
      return;
    }
    final distance = _textScroll.position.maxScrollExtent;
    if (distance <= 0) {
      return; // A short chapter stays unread until explicitly marked complete.
    }
    final offset = _ratio(_textScroll.offset / distance);
    if ((offset - _offset).abs() < .001) return;
    setState(() => _offset = offset);
    _changed();
  }

  void _changed() {
    _dirty = true;
    _saveTimer?.cancel();
    _saveTimer = Timer(const Duration(milliseconds: 700), _flush);
  }

  Future<void> _flush() async {
    _saveTimer?.cancel();
    if (_dirty && _item.readable && !_loading && _error == null) {
      _pending = _ProgressSave(_item.id, _position);
      _dirty = false;
    }
    if (_saving) {
      await _saveFuture;
      return;
    }
    if (_pending == null) return;
    _saving = true;
    if (mounted) setState(() {});
    _saveFuture = _drainSaves();
    await _saveFuture;
  }

  Future<void> _drainSaves() async {
    try {
      while (_pending != null) {
        final save = _pending!;
        _pending = null;
        try {
          await widget.api.put(
            '/items/${save.itemId}/progress',
            save.position.toJson(),
          );
          widget.onProgressSaved?.call(save.itemId, save.position);
          if (mounted) setState(() => _saveError = null);
        } catch (e) {
          // Keep the newest pending snapshot, including its original item id.
          _pending ??= save;
          if (mounted) setState(() => _saveError = '阅读进度未保存：${_readError(e)}');
          break;
        }
      }
    } finally {
      _saving = false;
      if (mounted) setState(() {});
    }
  }

  Future<bool> _ensureSaved() async {
    await _flush();
    if (_dirty && _saveError == null) await _flush();
    if (!mounted) return false;
    if (_saveError == null && !_dirty && _pending == null) return true;
    final discard = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('阅读进度尚未保存'),
        content: Text(_saveError ?? '请保存进度后继续。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('放弃本次进度'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('返回并重试'),
          ),
        ],
      ),
    );
    if (discard == true) {
      _pending = null;
      _dirty = false;
      _saveError = null;
      return true;
    }
    return false;
  }

  Future<void> _leave() async {
    if (_leaving) return;
    setState(() => _leaving = true);
    await _videoPlayback.pause();
    if (!mounted) return;
    final saved = await _ensureSaved();
    if (!mounted) return;
    if (!saved) {
      setState(() => _leaving = false);
      return;
    }
    setState(() => _allowPop = true);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) Navigator.pop(context);
    });
  }

  Future<void> _changeAsset(
    int page, {
    int section = 0,
    double offset = 0,
  }) async {
    if (_loading || page < 0 || page >= _item.assets.length) return;
    final navigation = ++_assetNavigation;
    await _videoPlayback.pause();
    if (!mounted || navigation != _assetNavigation || _leaving) return;
    // Capture the old position without making page gestures wait for the network.
    unawaited(_flush());
    final wasText = _text;
    _loadVersion++;
    setState(() {
      _page = page;
      _chapter = section;
      _offset = offset;
      _error = null;
    });
    if (_text) {
      await _loadText(section, offset, markPosition: true);
    } else {
      if (_flow) {
        _flowController.jumpTo(page);
      } else if (wasText) {
        final oldPages = _pages;
        _pages = PageController(initialPage: page);
        WidgetsBinding.instance.addPostFrameCallback((_) => oldPages.dispose());
      } else if (_pages.hasClients && _pages.page?.round() != page) {
        _pages.jumpToPage(page);
      }
      if (!_video) _changed();
    }
  }

  void _imagePageChanged(int page) {
    if (page == _page) return;
    unawaited(_changeAsset(page));
  }

  Future<void> _changeChapter(int chapter, {double offset = 0}) async {
    if (_loading || chapter < 0 || chapter >= _chapters.length) return;
    unawaited(_flush());
    setState(() {
      _restoring = true;
      _chapter = chapter;
      _offset = offset;
      _textRestoreRevision++;
    });
    _changed();
  }

  void _font(double change) {
    _fontTouched = true;
    setState(() {
      _restoring = true;
      _fontSize = (_fontSize + change).clamp(15, 30);
    });
    unawaited(widget.preferences.saveFontSize(_fontSize).catchError((_) {}));
  }

  Future<void> _directory() async {
    if (_loading || _item.assets.isEmpty) return;
    await _videoPlayback.pause();
    if (!mounted) return;
    final jump = await showModalBottomSheet<_ReaderJump>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (context) => DraggableScrollableSheet(
        expand: false,
        initialChildSize: .7,
        minChildSize: .3,
        maxChildSize: .94,
        builder: (context, scroll) => Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 0, 20, 12),
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      _text ? '章节目录' : '页面目录',
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                  ),
                  Text(
                    _text
                        ? '${_chapters.length} 节'
                        : '${_item.assets.length} P',
                  ),
                ],
              ),
            ),
            if (_text && _item.assets.length > 1)
              Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: 20,
                  vertical: 8,
                ),
                child: DropdownButtonFormField<int>(
                  initialValue: _page,
                  isExpanded: true,
                  decoration: const InputDecoration(
                    labelText: '正文文件',
                    border: OutlineInputBorder(),
                  ),
                  items: [
                    for (int i = 0; i < _item.assets.length; i++)
                      DropdownMenuItem(
                        value: i,
                        child: Text(
                          _item.assets[i].name.isEmpty
                              ? '第 ${i + 1} 个文件'
                              : _item.assets[i].name,
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                  ],
                  onChanged: (page) {
                    if (page != null && page != _page) {
                      Navigator.pop(context, _ReaderJump(page, 0));
                    }
                  },
                ),
              ),
            Expanded(
              child: ListView.builder(
                controller: scroll,
                itemCount: _text ? _chapters.length : _item.assets.length,
                itemBuilder: (_, index) => ListTile(
                  selected: _text ? index == _chapter : index == _page,
                  leading: Text('${index + 1}'),
                  title: Text(
                    _text
                        ? (_chapters[index].title.isEmpty
                              ? '第 ${index + 1} 节'
                              : _chapters[index].title)
                        : '第 ${index + 1} P',
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                  ),
                  trailing: (_text ? index == _chapter : index == _page)
                      ? const Icon(Icons.bookmark)
                      : null,
                  onTap: () => Navigator.pop(
                    context,
                    _ReaderJump(_text ? _page : index, _text ? index : 0),
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
    if (!mounted || jump == null) return;
    if (jump.page == _page && _text) {
      await _changeChapter(jump.section);
    } else {
      await _changeAsset(jump.page, section: jump.section);
    }
  }

  Future<void> _series() async {
    await _videoPlayback.pause();
    if (!mounted) return;
    final id = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => _ReaderSeries(api: widget.api, item: _item),
    );
    if (!mounted || id == null || id == _item.id) return;
    if (!await _ensureSaved() || !mounted) return;
    setState(() => _loading = true);
    try {
      final data = await widget.api.get('/items/$id') as Map<String, dynamic>;
      if (!mounted) return;
      final item = LibraryItem.fromJson(data);
      if (!item.readable) {
        setState(() {
          _loading = false;
          if (_text) {
            _restoring = true;
            _textRestoreRevision++;
          }
        });
        await _unready(item);
        return;
      }
      _loadVersion++;
      final oldPages = _pages;
      setState(() {
        _item = item;
        _flow = _preferredFlow && _canFlow;
        _page = _bounded(item.progress.page, item.assets.length);
        _chapter = item.progress.section;
        _offset = _ratio(item.progress.offset);
        _pages = PageController(initialPage: _page);
        _chapters = [];
        _loading = false;
        _error = null;
        _saveError = null;
      });
      WidgetsBinding.instance.addPostFrameCallback((_) => oldPages.dispose());
      if (_text) await _loadText(_chapter, _offset);
      if (!_text && !_video) _changed();
    } catch (e) {
      if (mounted) {
        setState(() {
          _loading = false;
          if (_text) {
            _restoring = true;
            _textRestoreRevision++;
          }
        });
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(_readError(e))));
      }
    }
  }

  Future<void> _unready(LibraryItem item) async {
    bool busy = false,
        queued = ['queued', 'downloading'].contains(item.contentState);
    String? error;
    await showDialog<void>(
      context: context,
      builder: (dialogContext) => StatefulBuilder(
        builder: (context, update) => AlertDialog(
          title: Text(item.displayTitle),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(queued ? '正文正在服务器下载队列中，稍后从系列目录重新打开。' : '正文尚不可阅读，封面不包含正文。'),
              if (error != null)
                Padding(
                  padding: const EdgeInsets.only(top: 12),
                  child: Text(
                    error!,
                    style: TextStyle(
                      color: Theme.of(context).colorScheme.error,
                    ),
                  ),
                ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: busy ? null : () => Navigator.pop(dialogContext),
              child: const Text('关闭'),
            ),
            if (!queued && item.source != 'local')
              FilledButton(
                onPressed: busy
                    ? null
                    : () async {
                        update(() {
                          busy = true;
                          error = null;
                        });
                        try {
                          await widget.api.post(
                            '/items/${item.id}/download',
                            {},
                          );
                          if (dialogContext.mounted) {
                            update(() {
                              queued = true;
                              busy = false;
                            });
                          }
                        } catch (e) {
                          if (dialogContext.mounted) {
                            update(() {
                              error = _readError(e);
                              busy = false;
                            });
                          }
                        }
                      },
                child: Text(busy ? '正在提交…' : '请求服务器下载正文'),
              ),
          ],
        ),
      ),
    );
  }

  Future<void> _previous() async {
    if (_text && _chapter > 0) {
      await _changeChapter(_chapter - 1);
    } else if (_page > 0) {
      await _changeAsset(_page - 1);
    }
  }

  Future<void> _next() async {
    if (_text && _chapter < _chapters.length - 1) {
      await _changeChapter(_chapter + 1);
    } else if (_page < _item.assets.length - 1) {
      await _changeAsset(_page + 1);
    }
  }

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    final atEnd =
        _text &&
        !_loading &&
        _chapters.isNotEmpty &&
        _chapter == _chapters.length - 1 &&
        _page == _item.assets.length - 1;
    return PopScope(
      canPop: _allowPop,
      onPopInvokedWithResult: (didPop, result) {
        if (!didPop) _leave();
      },
      child: Scaffold(
        appBar: AppBar(
          leading: IconButton(
            tooltip: '返回书库',
            onPressed: _leaving ? null : _leave,
            icon: const Icon(Icons.arrow_back),
          ),
          title: Text(
            _item.displayTitle,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
          ),
          actions: [
            if (_item.series.isNotEmpty)
              IconButton(
                tooltip: '系列与上下篇',
                onPressed: _loading ? null : _series,
                icon: const Icon(Icons.library_books_outlined),
              ),
            if (_canFlow)
              PopupMenuButton<bool>(
                tooltip: '阅读方式',
                icon: Icon(
                  _flow ? Icons.view_stream_outlined : Icons.swipe_outlined,
                ),
                initialValue: _flow,
                onSelected: _setFlow,
                itemBuilder: (_) => [
                  PopupMenuItem(value: false, child: const Text('横向翻页')),
                  PopupMenuItem(value: true, child: const Text('竖向瀑布流')),
                ],
              ),
            IconButton(
              tooltip: _text ? '章节目录' : '页面目录',
              onPressed: _loading ? null : _directory,
              icon: const Icon(Icons.list_rounded),
            ),
            if (_text)
              PopupMenuButton<double>(
                tooltip: '文字大小',
                onSelected: _font,
                itemBuilder: (_) => [
                  PopupMenuItem(
                    value: 1,
                    enabled: _fontSize < 30,
                    child: const Text('增大字号'),
                  ),
                  PopupMenuItem(
                    value: -1,
                    enabled: _fontSize > 15,
                    child: const Text('减小字号'),
                  ),
                ],
                icon: const Icon(Icons.text_fields),
              ),
          ],
        ),
        body: IgnorePointer(
          ignoring: _leaving,
          child: Stack(
            fit: StackFit.expand,
            children: [
              // Save feedback must not resize or recreate the reading viewport.
              // In particular, a late failure near the bottom of a comic must
              // not change its scroll extents while the reader reverses a drag.
              Column(
                children: [
                  Expanded(
                    child: !_item.readable
                        ? const Center(child: Text('正文尚不可阅读，请返回作品详情'))
                        : _loading || (!_text && !_modeReady)
                        ? const Center(child: CircularProgressIndicator())
                        : _error != null
                        ? Center(
                            child: Padding(
                              padding: const EdgeInsets.all(24),
                              child: Column(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  Text(_error!, textAlign: TextAlign.center),
                                  const SizedBox(height: 12),
                                  FilledButton(
                                    onPressed: () =>
                                        _loadText(_chapter, _offset),
                                    child: const Text('重试'),
                                  ),
                                ],
                              ),
                            ),
                          )
                        : _text
                        ? _chapters.isEmpty
                              ? const SizedBox.shrink()
                              : SelectionArea(
                                  child: NovelReaderBody(
                                    key: ValueKey((_item.id, _page)),
                                    text: _chapters[_chapter].text,
                                    title: _chapters[_chapter].title.isEmpty
                                        ? '第 ${_chapter + 1} 节'
                                        : _chapters[_chapter].title,
                                    fontSize: _fontSize,
                                    restoreOffset: _offset,
                                    restoreRevision: _textRestoreRevision,
                                    controller: _textScroll,
                                    onLayoutStart: () => _restoring = true,
                                    onLayoutReady: () => _restoring = false,
                                    footer: atEnd
                                        ? OutlinedButton.icon(
                                            onPressed: () {
                                              setState(() => _offset = 1);
                                              _changed();
                                              _flush();
                                            },
                                            icon: const Icon(Icons.done_all),
                                            label: Text(
                                              _offset >= 1
                                                  ? '已到达末尾 · 再次保存'
                                                  : '标记读完',
                                            ),
                                          )
                                        : FilledButton.tonalIcon(
                                            onPressed: _next,
                                            icon: const Icon(
                                              Icons.arrow_forward,
                                            ),
                                            label: const Text('下一节'),
                                          ),
                                  ),
                                )
                        : _flow
                        ? ComicFlowReader(
                            key: ValueKey(_item.id),
                            api: widget.api,
                            itemId: _item.id,
                            assets: _item.assets,
                            initialPage: _page,
                            initialOffset: _offset,
                            controller: _flowController,
                            onPosition: _flowPosition,
                          )
                        : PageView.builder(
                            key: ValueKey(_item.id),
                            controller: _pages,
                            itemCount: _item.assets.length,
                            onPageChanged: _imagePageChanged,
                            itemBuilder: (_, page) {
                              final asset = _item.assets[page];
                              if (asset.mimeType.startsWith('video/')) {
                                final itemId = _item.id;
                                return VideoAssetPlayer(
                                  key: ValueKey((_item.id, asset.id)),
                                  api: widget.api,
                                  assetId: asset.id,
                                  active: page == _page,
                                  initialOffset: page == _page ? _offset : 0,
                                  playback: _videoPlayback,
                                  onPosition: (offset, immediate) =>
                                      _videoPosition(
                                        itemId,
                                        page,
                                        offset,
                                        immediate,
                                      ),
                                );
                              }
                              if (!asset.mimeType.startsWith('image/')) {
                                return Center(
                                  child: TextButton(
                                    onPressed: () => _changeAsset(page),
                                    child: const Text('打开正文文件'),
                                  ),
                                );
                              }
                              return InteractiveViewer(
                                minScale: 1,
                                maxScale: 5,
                                child: SizedBox.expand(
                                  child: ApiImage(
                                    key: ValueKey(asset.id),
                                    api: widget.api,
                                    path: '/assets/${asset.id}/content',
                                    fit: BoxFit.contain,
                                  ),
                                ),
                              );
                            },
                          ),
                  ),
                  SafeArea(
                    top: false,
                    child: Padding(
                      padding: const EdgeInsets.fromLTRB(8, 4, 8, 6),
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Row(
                            children: [
                              IconButton(
                                tooltip: _text ? '上一节' : '上一 P',
                                onPressed:
                                    !_loading &&
                                        (_page > 0 || (_text && _chapter > 0))
                                    ? _previous
                                    : null,
                                icon: const Icon(Icons.chevron_left),
                              ),
                              Expanded(
                                child: TextButton(
                                  onPressed: _loading ? null : _directory,
                                  child: Text(
                                    _text
                                        ? '第 ${_chapter + 1} / ${_chapters.length} 节'
                                        : '第 ${_page + 1} / ${_item.assets.length} P',
                                    textAlign: TextAlign.center,
                                  ),
                                ),
                              ),
                              IconButton(
                                tooltip: _text ? '下一节' : '下一 P',
                                onPressed:
                                    !_loading &&
                                        (_page < _item.assets.length - 1 ||
                                            (_text &&
                                                _chapter <
                                                    _chapters.length - 1))
                                    ? _next
                                    : null,
                                icon: const Icon(Icons.chevron_right),
                              ),
                            ],
                          ),
                          Text(
                            '${(_percent * 100).round()}%${_saving
                                ? ' · 保存中…'
                                : _saveError != null
                                ? ' · 进度未保存'
                                : _dirty
                                ? ' · 待保存'
                                : ''}',
                            style: TextStyle(
                              fontSize: 13,
                              color: colors.onSurfaceVariant,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ],
              ),
              if (_saveError != null)
                Positioned(
                  top: 0,
                  left: 0,
                  right: 0,
                  child: Material(
                    color: colors.errorContainer,
                    elevation: 2,
                    child: Padding(
                      padding: const EdgeInsets.fromLTRB(16, 4, 8, 4),
                      child: Row(
                        children: [
                          Expanded(
                            child: Text(
                              _saveError!,
                              style: TextStyle(
                                fontSize: 13,
                                color: colors.onErrorContainer,
                              ),
                            ),
                          ),
                          TextButton(
                            onPressed: _saving ? null : _flush,
                            child: const Text('重试'),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ProgressSave {
  final String itemId;
  final ReadingPosition position;
  _ProgressSave(this.itemId, this.position);
}

class _ReaderJump {
  final int page, section;
  _ReaderJump(this.page, this.section);
}

class _ReaderSeries extends StatefulWidget {
  final LibraryApi api;
  final LibraryItem item;
  const _ReaderSeries({required this.api, required this.item});
  @override
  State<_ReaderSeries> createState() => _ReaderSeriesState();
}

class _ReaderSeriesState extends State<_ReaderSeries> {
  late String _seriesId;
  List<Map<String, dynamic>> _entries = [];
  bool _loading = true, _complete = true;
  String? _error;
  int _request = 0;
  @override
  void initState() {
    super.initState();
    _seriesId = widget.item.series.first.id;
    _load();
  }

  Future<void> _load() async {
    final request = ++_request;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final data =
          await widget.api.get('/series/$_seriesId') as Map<String, dynamic>;
      if (!mounted || request != _request) return;
      setState(() {
        _entries = (data['entries'] as List).cast<Map<String, dynamic>>()
          ..sort(
            (a, b) => (a['position'] as num).compareTo(b['position'] as num),
          );
        _complete = data['complete'] == true;
        _loading = false;
      });
    } catch (e) {
      if (mounted && request == _request) {
        setState(() {
          _error = _readError(e);
          _loading = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final current = _entries.indexWhere((e) => e['item_id'] == widget.item.id);
    return DraggableScrollableSheet(
      expand: false,
      initialChildSize: .75,
      minChildSize: .35,
      maxChildSize: .94,
      builder: (context, scroll) => Column(
        children: [
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 20),
            child: DropdownButtonFormField<String>(
              initialValue: _seriesId,
              isExpanded: true,
              decoration: const InputDecoration(
                labelText: '系列与上下篇',
                border: OutlineInputBorder(),
              ),
              items: widget.item.series
                  .map(
                    (s) => DropdownMenuItem(
                      value: s.id,
                      child: Text(
                        s.title,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                  )
                  .toList(),
              onChanged: (id) {
                if (id != null) {
                  _seriesId = id;
                  _load();
                }
              },
            ),
          ),
          if (!_complete)
            const Padding(
              padding: EdgeInsets.all(12),
              child: Text('目录不完整，可能达到单次获取上限。'),
            ),
          if (!_loading && _error == null && current >= 0)
            Row(
              children: [
                Expanded(
                  child: TextButton.icon(
                    onPressed: current > 0
                        ? () => Navigator.pop(
                            context,
                            _entries[current - 1]['item_id'],
                          )
                        : null,
                    icon: const Icon(Icons.chevron_left),
                    label: const Text('上一篇'),
                  ),
                ),
                Expanded(
                  child: TextButton.icon(
                    onPressed: current < _entries.length - 1
                        ? () => Navigator.pop(
                            context,
                            _entries[current + 1]['item_id'],
                          )
                        : null,
                    icon: const Icon(Icons.chevron_right),
                    label: const Text('下一篇'),
                  ),
                ),
              ],
            ),
          Expanded(
            child: _loading
                ? const Center(child: CircularProgressIndicator())
                : _error != null
                ? Center(
                    child: Padding(
                      padding: const EdgeInsets.all(20),
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Text(_error!),
                          TextButton(onPressed: _load, child: const Text('重试')),
                        ],
                      ),
                    ),
                  )
                : _entries.isEmpty
                ? const Center(child: Text('系列暂无成员'))
                : ListView.builder(
                    controller: scroll,
                    itemCount: _entries.length,
                    itemBuilder: (_, index) {
                      final entry = _entries[index];
                      final selected = entry['item_id'] == widget.item.id;
                      final ready =
                          entry['content_state'] == 'ready' &&
                          ((entry['page_count'] as num?)?.toInt() ?? 0) > 0;
                      return ListTile(
                        selected: selected,
                        leading: Text('${index + 1}'),
                        title: Text(
                          entry['title'] as String? ?? '未命名',
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                        ),
                        subtitle: Text(
                          [
                            if ((entry['label'] as String? ?? '').isNotEmpty)
                              entry['label'] as String,
                            ready ? '可阅读' : '正文未就绪',
                          ].join(' · '),
                        ),
                        trailing: selected
                            ? const Icon(Icons.bookmark)
                            : Icon(
                                ready
                                    ? Icons.chevron_right
                                    : Icons.cloud_download_outlined,
                              ),
                        onTap: selected
                            ? null
                            : () => Navigator.pop(context, entry['item_id']),
                      );
                    },
                  ),
          ),
        ],
      ),
    );
  }
}
