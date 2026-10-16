import 'dart:async';
import 'package:flutter/material.dart';
import 'api.dart';
import 'api_image.dart';
import 'models.dart';
import 'reader_screen.dart';
import 'tag_browser_screen.dart';
import 'author_screen.dart';
import 'characters_screen.dart';
import 'favorites_screen.dart';
import 'score_editor.dart';
import 'item_cover.dart';
import 'source_labels.dart';
import 'admin_screen.dart';

const _sources = librarySources;
String _stateLabel(String state) => switch (state) {
  'ready' => '可阅读',
  'queued' => '等待服务器下载',
  'downloading' => '服务器下载中',
  'failed' => '正文不可用',
  _ => '仅封面与资料',
};
String _message(Object error) =>
    error is ApiFailure ? error.message : '无法读取内容，请重试';

class LibraryScreen extends StatefulWidget {
  final LibraryApi api;
  final VoidCallback? onSettings;
  final LibraryTag? initialTag;
  final String initialAuthor, initialSource;
  final String? initialFavoriteGroup;
  final String initialFavoriteName;
  final bool initialHistory;
  const LibraryScreen({
    super.key,
    required this.api,
    this.onSettings,
    this.initialTag,
    this.initialAuthor = '',
    this.initialSource = '',
    this.initialFavoriteGroup,
    this.initialFavoriteName = '全部收藏',
    this.initialHistory = false,
  });
  @override
  State<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends State<LibraryScreen> {
  final _search = TextEditingController();
  final _scroll = ScrollController();
  Timer? _debounce;
  String _source = '', _type = '', _query = '';
  String _author = '';
  LibraryTag? _tag;
  String? _error;
  int _page = 1, _total = 0, _request = 0;
  bool _loading = true, _refreshFailed = false;
  bool _downloaded = false;
  String? _favoriteGroup;
  bool _history = false;
  List<LibraryItem> _items = [];
  int get _pages => (_total / 24).ceil().clamp(1, 100000000);

  @override
  void initState() {
    super.initState();
    _source = widget.initialSource;
    _author = widget.initialAuthor;
    _tag = widget.initialTag;
    _favoriteGroup = widget.initialFavoriteGroup;
    _history = widget.initialHistory;
    _scroll.addListener(_onScroll);
    _load();
  }

  void _onScroll() {
    if (_scroll.hasClients &&
        _scroll.position.extentAfter < 400 &&
        !_loading &&
        _error == null &&
        _page < _pages) {
      _load(append: true);
    }
  }

  @override
  void dispose() {
    _request++;
    _debounce?.cancel();
    _search.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _load({
    bool reset = false,
    bool append = false,
    bool tail = false,
  }) async {
    if (append && (_loading || _page >= _pages)) return;
    final targetPage = append
        ? _page + 1
        : tail
        ? _page
        : 1;
    final request = ++_request;
    setState(() {
      _loading = true;
      _error = null;
      _refreshFailed = false;
      if (reset && !append && !tail) {
        _items = [];
        _page = 1;
      }
    });
    try {
      final data =
          await widget.api.get(
                '/items',
                query: {
                  'page': '$targetPage',
                  'page_size': '24',
                  'sort': 'latest',
                  if (_downloaded) 'downloaded': 'true',
                  if (_favoriteGroup != null) 'favorite': 'true',
                  if (_favoriteGroup?.isNotEmpty == true)
                    'favorite_group': _favoriteGroup!,
                  if (_history) 'history': 'true',
                  if (_query.isNotEmpty) 'q': _query,
                  if (_source.isNotEmpty) 'source': _source,
                  if (_type.isNotEmpty) 'type': _type,
                  if (_author.isNotEmpty) 'author': _author,
                  if (_tag != null) 'tag': _tag!.id,
                },
              )
              as Map<String, dynamic>;
      if (!mounted || request != _request) return;
      _total = (data['total'] as num).toInt();
      setState(() {
        final incoming = (data['items'] as List).map(
          (v) => LibraryItem.fromJson(v as Map<String, dynamic>),
        );
        _items = {
          for (final item in [
            ...(append || tail ? _items : <LibraryItem>[]),
            ...incoming,
          ])
            item.id: item,
        }.values.toList();
        _page = targetPage;
        _loading = false;
      });
    } catch (e) {
      if (mounted && request == _request) {
        setState(() {
          _error = _message(e);
          _refreshFailed = !append && !tail;
          _loading = false;
        });
      }
    }
  }

  void _searchChanged(String value) {
    _debounce?.cancel();
    _request++;
    _debounce = Timer(const Duration(milliseconds: 350), () {
      _query = value.trim();
      _load(reset: true);
    });
  }

  Future<void> _open(LibraryItem item) async {
    await Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => _ItemDetailScreen(
          api: widget.api,
          itemId: item.id,
          onChanged: (updated) {
            if (!mounted) return;
            bool removed = false;
            setState(() {
              final remove =
                  _favoriteGroup != null &&
                  (!updated.isFavorite ||
                      (_favoriteGroup == 'ungrouped' &&
                          updated.favoriteGroupIds.isNotEmpty) ||
                      (_favoriteGroup!.isNotEmpty &&
                          _favoriteGroup != 'ungrouped' &&
                          !updated.favoriteGroupIds.contains(_favoriteGroup)));
              if (remove && _items.any((item) => item.id == updated.id)) {
                removed = true;
                _total--;
              }
              _items = [
                for (final current in _items)
                  if (current.id != updated.id)
                    current
                  else if (!remove)
                    updated,
              ];
            });
            if (removed) unawaited(_load(tail: true));
          },
        ),
      ),
    );
  }

  Future<void> _browseTags() async {
    final tag = await Navigator.of(context).push<LibraryTag>(
      MaterialPageRoute(
        builder: (_) => TagBrowserScreen(api: widget.api, source: _source),
      ),
    );
    if (!mounted || tag == null) return;
    setState(() {
      _tag = tag;
      _author = '';
    });
    _load(reset: true);
  }

  void _openAuthors() {
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => AuthorsScreen(
          api: widget.api,
          onOpenItem: (id) => Navigator.of(context).push(
            MaterialPageRoute<void>(
              builder: (_) => _ItemDetailScreen(api: widget.api, itemId: id),
            ),
          ),
        ),
      ),
    );
  }

  void _chooseSource(String source) {
    Navigator.pop(context);
    setState(() {
      _source = source;
      if (source.isEmpty) _downloaded = false;
      _tag = null;
      _author = '';
    });
    _load(reset: true);
  }

  void _favorites() {
    Navigator.pop(context);
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => FavoriteGroupsScreen(
          api: widget.api,
          onOpen: (id, name) => Navigator.of(context).push(
            MaterialPageRoute<void>(
              builder: (_) => LibraryScreen(
                api: widget.api,
                initialFavoriteGroup: id,
                initialFavoriteName: name,
              ),
            ),
          ),
        ),
      ),
    );
  }

  Future<void> _clearHistory() async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('清空浏览历史？'),
        content: const Text('清除书库服务器上的浏览记录，作品、收藏和阅读进度保留。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('清空历史'),
          ),
        ],
      ),
    );
    if (!mounted || yes != true) return;
    try {
      await widget.api.delete('/history');
      if (mounted) _load(reset: true);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(_message(e))));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    return Scaffold(
      drawer: Drawer(
        child: SafeArea(
          child: ListView(
            padding: EdgeInsets.zero,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(24, 24, 16, 16),
                child: Row(
                  children: [
                    Icon(Icons.auto_stories_rounded, color: colors.primary),
                    const SizedBox(width: 12),
                    Text(
                      '栖页',
                      style: Theme.of(context).textTheme.headlineSmall,
                    ),
                  ],
                ),
              ),
              for (final entry in _sources.entries)
                ListTile(
                  leading: Icon(
                    entry.key.isEmpty
                        ? Icons.library_books_outlined
                        : Icons.folder_outlined,
                  ),
                  title: Text(entry.value),
                  selected:
                      !_downloaded &&
                      _source == entry.key &&
                      _tag == null &&
                      _author.isEmpty,
                  onTap: () => _chooseSource(entry.key),
                ),
              ListTile(
                leading: const Icon(Icons.download_done_rounded),
                title: const Text('已下载'),
                selected: _downloaded,
                onTap: () {
                  Navigator.pop(context);
                  setState(() {
                    _downloaded = true;
                    _source = '';
                    _tag = null;
                    _author = '';
                  });
                  _load(reset: true);
                },
              ),
              const Divider(),
              ListTile(
                leading: const Icon(Icons.favorite_outline),
                title: const Text('我的收藏'),
                onTap: _favorites,
              ),
              ListTile(
                leading: const Icon(Icons.history),
                title: const Text('浏览历史'),
                onTap: () {
                  Navigator.pop(context);
                  Navigator.of(context).push(
                    MaterialPageRoute<void>(
                      builder: (_) =>
                          LibraryScreen(api: widget.api, initialHistory: true),
                    ),
                  );
                },
              ),
              ExpansionTile(
                leading: const Icon(Icons.filter_list),
                title: const Text('内容类型'),
                children: [
                  for (final entry in const {
                    '': '全部类型',
                    'manga': '漫画',
                    'illustration': '插画',
                    'novel': '小说',
                    'article': '文章',
                  }.entries)
                    ListTile(
                      contentPadding: const EdgeInsets.only(
                        left: 72,
                        right: 16,
                      ),
                      title: Text(entry.value),
                      selected: _type == entry.key,
                      onTap: () {
                        Navigator.pop(context);
                        setState(() => _type = entry.key);
                        _load(reset: true);
                      },
                    ),
                ],
              ),
              ListTile(
                leading: const Icon(Icons.people_outline),
                title: const Text('作者与收藏'),
                onTap: () {
                  Navigator.pop(context);
                  _openAuthors();
                },
              ),
              ListTile(
                leading: const Icon(Icons.portrait_outlined),
                title: const Text('人物作品墙'),
                onTap: () {
                  Navigator.pop(context);
                  Navigator.of(context).push(
                    MaterialPageRoute<void>(
                      builder: (_) => CharactersScreen(
                        api: widget.api,
                        onOpenItem: (id) => Navigator.of(context).push(
                          MaterialPageRoute<void>(
                            builder: (_) =>
                                _ItemDetailScreen(api: widget.api, itemId: id),
                          ),
                        ),
                      ),
                    ),
                  );
                },
              ),
              ListTile(
                leading: const Icon(Icons.tag),
                title: const Text('搜索标签'),
                onTap: () {
                  Navigator.pop(context);
                  _browseTags();
                },
              ),
              ListTile(
                leading: const Icon(Icons.admin_panel_settings_outlined),
                title: const Text('后台管理'),
                onTap: () {
                  Navigator.pop(context);
                  Navigator.of(context).push(
                    MaterialPageRoute<void>(
                      builder: (_) => AdminScreen(server: widget.api.server),
                    ),
                  );
                },
              ),
              if (widget.onSettings != null)
                ListTile(
                  leading: const Icon(Icons.settings_outlined),
                  title: const Text('服务器与更新'),
                  onTap: () {
                    Navigator.pop(context);
                    widget.onSettings!();
                  },
                ),
            ],
          ),
        ),
      ),
      appBar: AppBar(
        leading: Builder(
          builder: (context) => IconButton(
            tooltip: '打开侧边栏',
            icon: const Icon(Icons.menu),
            onPressed: () => Scaffold.of(context).openDrawer(),
          ),
        ),
        toolbarHeight: 68,
        titleSpacing: 0,
        title: Padding(
          padding: const EdgeInsets.only(right: 12),
          child: TextField(
            controller: _search,
            onChanged: _searchChanged,
            textInputAction: TextInputAction.search,
            onSubmitted: (value) {
              _debounce?.cancel();
              _query = value.trim();
              _load(reset: true);
            },
            decoration: InputDecoration(
              hintText: '搜索标题、作者或标签',
              prefixIcon: const Icon(Icons.search),
              suffixIcon: IconButton(
                tooltip: '清空搜索',
                onPressed: () {
                  _search.clear();
                  _searchChanged('');
                },
                icon: const Icon(Icons.close),
              ),
              filled: true,
              fillColor: colors.surfaceContainerHighest,
              contentPadding: const EdgeInsets.symmetric(
                horizontal: 16,
                vertical: 10,
              ),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(28),
                borderSide: BorderSide.none,
              ),
              enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(28),
                borderSide: BorderSide.none,
              ),
            ),
          ),
        ),
        actions: [
          if (_history)
            IconButton(
              tooltip: '清空浏览历史',
              onPressed: _clearHistory,
              icon: const Icon(Icons.delete_sweep_outlined),
            ),
          if (Navigator.canPop(context))
            IconButton(
              tooltip: '返回',
              onPressed: () => Navigator.pop(context),
              icon: const Icon(Icons.arrow_back),
            ),
        ],
      ),
      body: SafeArea(
        top: false,
        child: Column(
          children: [
            if (_tag != null ||
                _favoriteGroup != null ||
                _history ||
                _downloaded ||
                _author.isNotEmpty ||
                _source.isNotEmpty ||
                _type.isNotEmpty)
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: Wrap(
                    spacing: 8,
                    children: [
                      if (_favoriteGroup != null)
                        InputChip(
                          label: Text('我的收藏 · ${widget.initialFavoriteName}'),
                          onDeleted: () {
                            setState(() => _favoriteGroup = null);
                            _load(reset: true);
                          },
                        ),
                      if (_history)
                        InputChip(
                          label: const Text('浏览历史'),
                          onDeleted: () {
                            setState(() => _history = false);
                            _load(reset: true);
                          },
                        ),
                      if (_downloaded)
                        InputChip(
                          label: const Text('已下载'),
                          onDeleted: () {
                            setState(() => _downloaded = false);
                            _load(reset: true);
                          },
                        ),
                      if (_tag != null || _author.isNotEmpty)
                        InputChip(
                          label: Text(
                            _tag != null
                                ? '标签作品：${_tag!.displayName}'
                                : '同作者作品：$_author',
                          ),
                          onDeleted: () {
                            setState(() {
                              _tag = null;
                              _author = '';
                            });
                            _load(reset: true);
                          },
                        ),
                      if (_source.isNotEmpty)
                        InputChip(
                          label: Text(_sources[_source]!),
                          onDeleted: () {
                            setState(() => _source = '');
                            _load(reset: true);
                          },
                        ),
                      if (_type.isNotEmpty)
                        InputChip(
                          label: Text(
                            const {
                                  'manga': '漫画',
                                  'illustration': '插画',
                                  'novel': '小说',
                                  'article': '文章',
                                }[_type] ??
                                _type,
                          ),
                          onDeleted: () {
                            setState(() => _type = '');
                            _load(reset: true);
                          },
                        ),
                    ],
                  ),
                ),
              ),
            Expanded(
              child: _loading && _items.isEmpty
                  ? const Center(child: CircularProgressIndicator())
                  : _error != null && _items.isEmpty
                  ? _Failure(message: _error!, retry: () => _load())
                  : _items.isEmpty
                  ? Center(
                      child: Padding(
                        padding: const EdgeInsets.all(24),
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Icon(
                              Icons.menu_book_outlined,
                              size: 48,
                              color: colors.onSurfaceVariant,
                            ),
                            const SizedBox(height: 16),
                            Text(
                              _query.isNotEmpty || _type.isNotEmpty
                                  ? '没有符合条件的作品'
                                  : '此书库还没有作品',
                              style: Theme.of(context).textTheme.titleMedium,
                            ),
                            const SizedBox(height: 8),
                            const Text('可在网页添加来源或导入文件。'),
                            TextButton.icon(
                              onPressed: () => _load(),
                              icon: const Icon(Icons.refresh),
                              label: const Text('刷新'),
                            ),
                          ],
                        ),
                      ),
                    )
                  : RefreshIndicator(
                      onRefresh: () => _load(),
                      child: LayoutBuilder(
                        builder: (context, box) {
                          final columns = box.maxWidth >= 1000
                              ? 5
                              : box.maxWidth >= 700
                              ? 4
                              : box.maxWidth >= 520
                              ? 3
                              : 2;
                          final scale =
                              MediaQuery.textScalerOf(context).scale(14) / 14;
                          return CustomScrollView(
                            controller: _scroll,
                            physics: const AlwaysScrollableScrollPhysics(),
                            slivers: [
                              SliverPadding(
                                padding: const EdgeInsets.fromLTRB(
                                  16,
                                  8,
                                  16,
                                  16,
                                ),
                                sliver: SliverGrid(
                                  gridDelegate:
                                      SliverGridDelegateWithFixedCrossAxisCount(
                                        crossAxisCount: columns,
                                        crossAxisSpacing: 12,
                                        mainAxisSpacing: 12,
                                        mainAxisExtent:
                                            ((box.maxWidth -
                                                        32 -
                                                        12 * (columns - 1)) /
                                                    columns) *
                                                1.25 +
                                            112 * scale,
                                      ),
                                  delegate: SliverChildBuilderDelegate((
                                    _,
                                    index,
                                  ) {
                                    final item = _items[index];
                                    return Card(
                                      clipBehavior: Clip.antiAlias,
                                      margin: EdgeInsets.zero,
                                      child: InkWell(
                                        onTap: () => _open(item),
                                        child: Column(
                                          crossAxisAlignment:
                                              CrossAxisAlignment.start,
                                          children: [
                                            Expanded(
                                              child: SizedBox(
                                                width: double.infinity,
                                                child: ItemCover(
                                                  api: widget.api,
                                                  item: item,
                                                ),
                                              ),
                                            ),
                                            Padding(
                                              padding: const EdgeInsets.all(10),
                                              child: Column(
                                                crossAxisAlignment:
                                                    CrossAxisAlignment.start,
                                                children: [
                                                  Text(
                                                    item.displayTitle,
                                                    maxLines: 2,
                                                    overflow:
                                                        TextOverflow.ellipsis,
                                                    style: Theme.of(
                                                      context,
                                                    ).textTheme.titleSmall,
                                                  ),
                                                  const SizedBox(height: 4),
                                                  Text(
                                                    item.author.isEmpty
                                                        ? _sources[item
                                                                  .source] ??
                                                              item.source
                                                        : item.author,
                                                    maxLines: 1,
                                                    overflow:
                                                        TextOverflow.ellipsis,
                                                    style: TextStyle(
                                                      fontSize: 13,
                                                      color: colors
                                                          .onSurfaceVariant,
                                                    ),
                                                  ),
                                                  const SizedBox(height: 5),
                                                  Text(
                                                    _stateLabel(
                                                      item.contentState,
                                                    ),
                                                    maxLines: 1,
                                                    overflow:
                                                        TextOverflow.ellipsis,
                                                    style: TextStyle(
                                                      fontSize: 13,
                                                      color:
                                                          item.contentState ==
                                                              'failed'
                                                          ? colors.error
                                                          : colors.primary,
                                                    ),
                                                  ),
                                                ],
                                              ),
                                            ),
                                          ],
                                        ),
                                      ),
                                    );
                                  }, childCount: _items.length),
                                ),
                              ),
                              if (_loading || _error != null || _page < _pages)
                                SliverToBoxAdapter(
                                  child: Padding(
                                    padding: const EdgeInsets.all(16),
                                    child: _loading
                                        ? const Center(
                                            child: SizedBox(
                                              width: 22,
                                              height: 22,
                                              child: CircularProgressIndicator(
                                                strokeWidth: 2,
                                              ),
                                            ),
                                          )
                                        : Center(
                                            child: TextButton(
                                              onPressed: () => _load(
                                                append: !_refreshFailed,
                                              ),
                                              child: Text(
                                                _error != null
                                                    ? (_refreshFailed
                                                          ? '刷新失败，重试'
                                                          : '加载失败，重试')
                                                    : '加载更多',
                                              ),
                                            ),
                                          ),
                                  ),
                                ),
                            ],
                          );
                        },
                      ),
                    ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ItemDetailScreen extends StatefulWidget {
  final LibraryApi api;
  final String itemId;
  final ValueChanged<LibraryItem>? onChanged;
  const _ItemDetailScreen({
    required this.api,
    required this.itemId,
    this.onChanged,
  });
  @override
  State<_ItemDetailScreen> createState() => _ItemDetailScreenState();
}

class _ItemDetailScreenState extends State<_ItemDetailScreen> {
  LibraryItem? _item;
  String? _error, _contentError, _actionError;
  bool _loading = true, _downloading = false, _refreshing = false;
  Timer? _poll;
  Timer? _translationPoll;
  bool _translating = false;
  bool _visited = false, _coverBusy = false;
  int _coverAttempts = 0;
  int _favoriteRevision = 0, _scoreRevision = 0;
  String? _translationError;
  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _poll?.cancel();
    _translationPoll?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    if (_refreshing) return;
    _refreshing = true;
    _poll?.cancel();
    final favoriteRevision = _favoriteRevision, scoreRevision = _scoreRevision;
    try {
      final data =
          await widget.api.get('/items/${widget.itemId}')
              as Map<String, dynamic>;
      if (!mounted) return;
      var updated = LibraryItem.fromJson(data);
      final current = _item;
      // A background refresh may predate a completed preference write.
      if (current != null && favoriteRevision != _favoriteRevision) {
        updated = updated.withFavorite({
          'is_favorite': current.isFavorite,
          'favorite_group_ids': current.favoriteGroupIds,
        });
      }
      if (current != null && scoreRevision != _scoreRevision) {
        updated = updated.withScore(current.userScore);
      }
      setState(() {
        _item = updated;
        _contentError = data['content_error'] as String?;
        _error = null;
        _loading = false;
      });
      widget.onChanged?.call(_item!);
      if (!_visited) {
        _visited = true;
        unawaited(_recordVisit());
      }
      _restoreTranslation();
      if (['queued', 'downloading'].contains(_item!.contentState) ||
          (_coverAttempts > 0 && _item!.coverUrl == null)) {
        if (_coverAttempts > 0) _coverAttempts--;
        _poll = Timer(const Duration(seconds: 6), _load);
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = _message(e);
          _loading = false;
        });
      }
    } finally {
      _refreshing = false;
    }
  }

  Future<void> _download() async {
    setState(() {
      _downloading = true;
      _actionError = null;
    });
    try {
      await widget.api.post('/items/${widget.itemId}/download', {});
      if (!mounted) return;
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('已提交服务器下载队列')));
      await _load();
    } catch (e) {
      if (mounted) setState(() => _actionError = _message(e));
    } finally {
      if (mounted) setState(() => _downloading = false);
    }
  }

  Future<void> _recordVisit() async {
    try {
      await widget.api.post('/items/${widget.itemId}/visit', {});
    } catch (_) {
      /* History availability never prevents opening or reading a work. */
    }
  }

  Future<void> _favorite() async {
    final item = _item;
    if (item == null) return;
    await editFavorite(
      context,
      widget.api,
      item,
      onSaved: (result) {
        _favoriteRevision++;
        final updated = (_item ?? item).withFavorite(result);
        if (mounted) setState(() => _item = updated);
        widget.onChanged?.call(updated);
      },
    );
  }

  Future<void> _score() async {
    final item = _item;
    if (item == null) return;
    await editScore(
      context,
      widget.api,
      item,
      onSaved: (score) {
        _scoreRevision++;
        final updated = (_item ?? item).withScore(score);
        if (mounted) setState(() => _item = updated);
        widget.onChanged?.call(updated);
      },
    );
  }

  Future<void> _refreshCover() async {
    if (_coverBusy) return;
    setState(() => _coverBusy = true);
    try {
      await widget.api.post('/items/${widget.itemId}/cover-refresh', {});
      if (!mounted) return;
      setState(() => _coverAttempts = 10);
      _poll?.cancel();
      _poll = Timer(const Duration(seconds: 6), _load);
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('已加入封面队列')));
    } catch (e) {
      if (mounted) setState(() => _actionError = _message(e));
    } finally {
      if (mounted) setState(() => _coverBusy = false);
    }
  }

  Future<void> _read({int? page}) async {
    final item = _item;
    if (item == null || !item.readable) return;
    await Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => ReaderScreen(
          api: widget.api,
          item: item,
          initialPage: page,
          onProgressSaved: (id, position) {
            if (!mounted || _item?.id != id) return;
            setState(() => _item = _item!.withProgress(position));
            widget.onChanged?.call(_item!);
          },
        ),
      ),
    );
  }

  void _openMember(String id) {
    if (id == widget.itemId) return;
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => _ItemDetailScreen(
          api: widget.api,
          itemId: id,
          onChanged: widget.onChanged,
        ),
      ),
    );
  }

  void _browse({LibraryTag? tag, String author = ''}) {
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => LibraryScreen(
          api: widget.api,
          initialTag: tag,
          initialAuthor: author,
          initialSource: _item!.source,
        ),
      ),
    );
  }

  Future<void> _restoreTranslation() async {
    if (_translationPoll != null || _translating) return;
    try {
      final state =
          await widget.api.get('/items/${widget.itemId}/translation') as Map;
      if (!mounted) return;
      if (['queued', 'running'].contains(state['status'])) {
        setState(() => _translating = true);
        _watchTranslation();
      } else if (state['status'] == 'failed') {
        setState(
          () => _translationError =
              state['error'] as String? ?? '汉化失败，请检查模型配置后重试。',
        );
      }
    } catch (_) {
      /* Older servers can still browse and read. */
    }
  }

  Future<void> _watchTranslation() async {
    _translationPoll = null;
    try {
      final state =
          await widget.api.get('/items/${widget.itemId}/translation') as Map;
      if (!mounted) return;
      if (['queued', 'running'].contains(state['status'])) {
        _translationPoll = Timer(const Duration(seconds: 3), _watchTranslation);
      } else {
        setState(() {
          _translating = false;
          _translationError = state['status'] == 'failed'
              ? state['error'] as String? ?? '汉化失败，请检查模型配置后重试。'
              : null;
        });
        await _load();
      }
    } catch (_) {
      if (mounted) {
        setState(() {
          _translating = false;
          _translationError = '无法读取汉化进度，请刷新后重试。';
        });
      }
    }
  }

  Future<void> _translate() async {
    if (_translating) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('AI 汉化标题和标签'),
        content: const Text(
          '将本作品的标题和标签发送到服务器配置的模型；使用远程接口时，这些文字会发送给对应服务商。中文译名单独保存，原文保留。',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('开始汉化'),
          ),
        ],
      ),
    );
    if (!mounted || confirmed != true || _translating) return;
    setState(() {
      _translating = true;
      _translationError = null;
    });
    try {
      await widget.api.post('/items/${widget.itemId}/translate', {
        'consent': true,
        'force': _item!.titleZh != null,
      });
      if (mounted) await _watchTranslation();
    } catch (e) {
      if (mounted) {
        setState(() {
          _translating = false;
          _translationError = e is ApiFailure && e.status == 503
              ? '请先在网页设置中配置并启用 AI 汉化模型。'
              : _message(e);
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final item = _item;
    final colors = Theme.of(context).colorScheme;
    return Scaffold(
      appBar: AppBar(
        title: const Text('作品详情'),
        actions: [
          if (item != null)
            IconButton(
              tooltip: item.isFavorite ? '管理收藏' : '收藏作品',
              onPressed: _favorite,
              icon: Icon(
                item.isFavorite ? Icons.favorite : Icons.favorite_outline,
              ),
            ),
          IconButton(
            tooltip: '刷新作品',
            onPressed: _loading ? null : _load,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : item == null
          ? _Failure(message: _error ?? '作品不存在', retry: _load)
          : RefreshIndicator(
              onRefresh: _load,
              child: ListView(
                padding: const EdgeInsets.all(20),
                children: [
                  if (_error != null) _Failure(message: _error!, retry: _load),
                  Center(
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(12),
                      child: SizedBox(
                        width: 170,
                        height: 220,
                        child: item.coverUrl == null
                            ? ColoredBox(
                                color: colors.surfaceContainerHighest,
                                child: const Icon(Icons.menu_book, size: 60),
                              )
                            : ApiImage(api: widget.api, path: item.coverUrl!),
                      ),
                    ),
                  ),
                  if (item.isText &&
                      item.coverUrl == null &&
                      item.source == 'pixiv')
                    TextButton.icon(
                      onPressed: _coverBusy || _coverAttempts > 0
                          ? null
                          : _refreshCover,
                      icon: const Icon(Icons.image_outlined),
                      label: Text(
                        _coverBusy || _coverAttempts > 0 ? '封面排队中…' : '获取小说封面',
                      ),
                    ),
                  const SizedBox(height: 20),
                  SelectableText(
                    item.displayTitle,
                    style: Theme.of(context).textTheme.headlineSmall,
                  ),
                  if (item.titleZh != null && item.titleZh != item.title) ...[
                    const SizedBox(height: 6),
                    SelectableText(
                      '原题：${item.title}',
                      style: TextStyle(color: colors.onSurfaceVariant),
                    ),
                  ],
                  const SizedBox(height: 8),
                  Text(
                    [
                      if (item.author.isNotEmpty) item.author,
                      _sources[item.source] ?? item.source,
                      item.isText ? '文字作品' : '${item.pageCount} P',
                    ].join(' · '),
                    style: TextStyle(color: colors.onSurfaceVariant),
                  ),
                  if (item.author.isNotEmpty)
                    Align(
                      alignment: Alignment.centerLeft,
                      child: TextButton.icon(
                        onPressed: () => _browse(author: item.author),
                        icon: const Icon(Icons.person_search_outlined),
                        label: const Text('查看同作者作品'),
                      ),
                    ),
                  if (item.source == 'pixiv')
                    Align(
                      alignment: Alignment.centerLeft,
                      child: TextButton.icon(
                        icon: const Icon(Icons.account_circle_outlined),
                        label: const Text('Pixiv 作者主页'),
                        onPressed: () => Navigator.of(context).push(
                          MaterialPageRoute<void>(
                            builder: (_) => item.authorSourceId != null
                                ? AuthorScreen(
                                    api: widget.api,
                                    target: item.authorSourceId!,
                                    onOpenItem: _openMember,
                                  )
                                : AuthorsScreen(
                                    api: widget.api,
                                    initialQuery: item.author,
                                    onOpenItem: _openMember,
                                  ),
                          ),
                        ),
                      ),
                    ),
                  const SizedBox(height: 16),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      Chip(label: Text(_stateLabel(item.contentState))),
                      if (item.readable) const Chip(label: Text('已保存到服务器')),
                      if (item.isFavorite)
                        const Chip(
                          avatar: Icon(Icons.favorite, size: 16),
                          label: Text('已收藏'),
                        ),
                      ActionChip(
                        avatar: Icon(
                          item.userScore > 0
                              ? Icons.star_rounded
                              : Icons.star_outline_rounded,
                          size: 18,
                        ),
                        label: Text(
                          item.userScore > 0
                              ? '我的评分 ${item.userScore}/5'
                              : '评分',
                        ),
                        onPressed: _score,
                      ),
                    ],
                  ),
                  if (_contentError?.isNotEmpty == true)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: Text(
                        _contentError!,
                        style: TextStyle(color: colors.error),
                      ),
                    ),
                  const SizedBox(height: 12),
                  if (item.readable)
                    FilledButton.icon(
                      onPressed: _read,
                      icon: const Icon(Icons.menu_book_outlined),
                      label: const Text('开始 / 继续阅读'),
                    )
                  else ...[
                    const Text('封面不包含正文，需由服务器下载后阅读。'),
                    const SizedBox(height: 10),
                    if (item.source != 'local')
                      FilledButton.icon(
                        onPressed:
                            _downloading ||
                                [
                                  'queued',
                                  'downloading',
                                ].contains(item.contentState)
                            ? null
                            : _download,
                        icon: _downloading
                            ? const SizedBox(
                                width: 18,
                                height: 18,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                ),
                              )
                            : const Icon(Icons.cloud_download_outlined),
                        label: Text(
                          _downloading
                              ? '正在提交…'
                              : [
                                  'queued',
                                  'downloading',
                                ].contains(item.contentState)
                              ? _stateLabel(item.contentState)
                              : '请求服务器下载正文',
                        ),
                      ),
                    if (item.source == 'local') const Text('请在网页检查文件或重新导入。'),
                  ],
                  if (_actionError != null)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: Text(
                        _actionError!,
                        style: TextStyle(color: colors.error),
                      ),
                    ),
                  const SizedBox(height: 12),
                  OutlinedButton.icon(
                    onPressed: _translating ? null : _translate,
                    icon: const Icon(Icons.translate),
                    label: Text(
                      _translating
                          ? '后台汉化中…'
                          : item.titleZh != null
                          ? '重新汉化标题和标签'
                          : 'AI 汉化标题和标签',
                    ),
                  ),
                  if (_translationError != null)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: Text(
                        _translationError!,
                        style: TextStyle(color: colors.error),
                      ),
                    ),
                  if (item.series.isNotEmpty) ...[
                    const SizedBox(height: 24),
                    Text(
                      '系列与上下篇',
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                    for (final series in item.series)
                      _SeriesSection(
                        api: widget.api,
                        series: series,
                        currentId: item.id,
                        onOpen: _openMember,
                      ),
                  ],
                  if (item.description.isNotEmpty) ...[
                    const SizedBox(height: 24),
                    Text('简介', style: Theme.of(context).textTheme.titleMedium),
                    const SizedBox(height: 8),
                    SelectableText(
                      item.description,
                      style: const TextStyle(height: 1.6),
                    ),
                  ],
                  if (item.tags.isNotEmpty) ...[
                    const SizedBox(height: 24),
                    Text('标签', style: Theme.of(context).textTheme.titleMedium),
                    const SizedBox(height: 8),
                    Wrap(
                      spacing: 6,
                      runSpacing: 4,
                      children: item.tagEntries
                          .map(
                            (tag) => Tooltip(
                              message: tag.name,
                              child: ActionChip(
                                label: Text(tag.displayName),
                                onPressed: () => _browse(tag: tag),
                              ),
                            ),
                          )
                          .toList(),
                    ),
                  ],
                  const SizedBox(height: 24),
                ],
              ),
            ),
    );
  }
}

class _SeriesSection extends StatefulWidget {
  final LibraryApi api;
  final SeriesLink series;
  final String currentId;
  final ValueChanged<String> onOpen;
  const _SeriesSection({
    required this.api,
    required this.series,
    required this.currentId,
    required this.onOpen,
  });
  @override
  State<_SeriesSection> createState() => _SeriesSectionState();
}

class _SeriesSectionState extends State<_SeriesSection> {
  List<Map<String, dynamic>>? _entries;
  String? _error;
  bool _loading = false, _complete = true, _expanded = false;
  int _request = 0;

  @override
  void didUpdateWidget(covariant _SeriesSection oldWidget) {
    super.didUpdateWidget(oldWidget);
    // A fresh detail response also refreshes member download states, even if
    // the series id and membership revision have not changed.
    if (oldWidget.series != widget.series || oldWidget.api != widget.api) {
      _request++;
      _entries = null;
      _error = null;
      _loading = false;
      _complete = true;
      if (_expanded) _load();
    }
  }

  Future<void> _load() async {
    if (_loading) return;
    final request = ++_request;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final data =
          await widget.api.get('/series/${widget.series.id}')
              as Map<String, dynamic>;
      if (!mounted || request != _request) return;
      setState(() {
        _entries = (data['entries'] as List).cast<Map<String, dynamic>>()
          ..sort(
            (a, b) => (a['position'] as num).compareTo(b['position'] as num),
          );
        _complete = data['complete'] == true;
      });
    } catch (e) {
      if (mounted && request == _request) setState(() => _error = _message(e));
    } finally {
      if (mounted && request == _request) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final entries = _entries ?? [];
    final index = entries.indexWhere((e) => e['item_id'] == widget.currentId);
    return ExpansionTile(
      tilePadding: EdgeInsets.zero,
      title: Text(widget.series.title),
      onExpansionChanged: (open) {
        _expanded = open;
        if (open && _entries == null) _load();
      },
      children: [
        if (_loading)
          const Padding(
            padding: EdgeInsets.all(16),
            child: CircularProgressIndicator(),
          ),
        if (_error != null) _Failure(message: _error!, retry: _load),
        if (!_complete)
          const Padding(
            padding: EdgeInsets.all(8),
            child: Text('目录不完整，可能达到单次获取上限。'),
          ),
        if (index >= 0)
          Row(
            children: [
              Expanded(
                child: TextButton.icon(
                  onPressed: index > 0
                      ? () => widget.onOpen(
                          entries[index - 1]['item_id'] as String,
                        )
                      : null,
                  icon: const Icon(Icons.chevron_left),
                  label: const Text('上一篇'),
                ),
              ),
              Expanded(
                child: TextButton.icon(
                  onPressed: index < entries.length - 1
                      ? () => widget.onOpen(
                          entries[index + 1]['item_id'] as String,
                        )
                      : null,
                  icon: const Icon(Icons.chevron_right),
                  label: const Text('下一篇'),
                ),
              ),
            ],
          ),
        if (entries.isNotEmpty)
          SizedBox(
            height: (entries.length * 80.0).clamp(80, 400),
            child: ListView.builder(
              itemCount: entries.length,
              itemBuilder: (_, i) {
                final entry = entries[i],
                    selected = entry['item_id'] == widget.currentId;
                return ListTile(
                  selected: selected,
                  leading: Text('${i + 1}'),
                  title: Text(
                    entry['title'] as String? ?? '未命名',
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                  ),
                  subtitle: Text(
                    [
                      if ((entry['label'] as String? ?? '').isNotEmpty)
                        entry['label'] as String,
                      _stateLabel(
                        entry['content_state'] as String? ?? 'metadata',
                      ),
                    ].join(' · '),
                  ),
                  trailing: selected
                      ? const Icon(Icons.bookmark)
                      : const Icon(Icons.chevron_right),
                  onTap: selected
                      ? null
                      : () => widget.onOpen(entry['item_id'] as String),
                );
              },
            ),
          ),
        if (_entries != null && entries.isEmpty)
          const Padding(padding: EdgeInsets.all(16), child: Text('系列暂无成员')),
      ],
    );
  }
}

class _Failure extends StatelessWidget {
  final String message;
  final VoidCallback retry;
  const _Failure({required this.message, required this.retry});
  @override
  Widget build(BuildContext context) => Center(
    child: Padding(
      padding: const EdgeInsets.all(20),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(
            message,
            textAlign: TextAlign.center,
            style: TextStyle(color: Theme.of(context).colorScheme.error),
          ),
          const SizedBox(height: 8),
          TextButton.icon(
            onPressed: retry,
            icon: const Icon(Icons.refresh),
            label: const Text('重试'),
          ),
        ],
      ),
    ),
  );
}
