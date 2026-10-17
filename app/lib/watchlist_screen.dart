import 'dart:async';
import 'package:flutter/material.dart';
import 'api.dart';

String _error(Object error) =>
    error is ApiFailure ? error.message : '无法读取追更，请重试';

class WatchlistScreen extends StatefulWidget {
  final LibraryApi api;
  final ValueChanged<String> onOpenItem;
  const WatchlistScreen({
    super.key,
    required this.api,
    required this.onOpenItem,
  });
  @override
  State<WatchlistScreen> createState() => _WatchlistScreenState();
}

class _WatchlistScreenState extends State<WatchlistScreen> {
  final _scroll = ScrollController(), _search = TextEditingController();
  String _kind = 'novel';
  bool _updates = false, _loading = false, _busy = false;
  int _page = 1, _total = 0, _request = 0;
  String? _failure, _message;
  List<Map<String, dynamic>> _rows = [];
  Timer? _debounce;
  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.position.extentAfter < 400 &&
          !_loading &&
          _rows.length < _total) {
        _load(append: true);
      }
    });
    _load();
  }

  @override
  void dispose() {
    _request++;
    _debounce?.cancel();
    _scroll.dispose();
    _search.dispose();
    super.dispose();
  }

  Future<void> _load({bool append = false}) async {
    if (append && (_loading || _rows.length >= _total)) return;
    final revision = ++_request, page = append ? _page + 1 : 1;
    setState(() {
      _loading = true;
      _failure = null;
    });
    try {
      final data =
          await widget.api.get(
                '/pixiv/watchlist',
                query: {
                  'kind': _kind,
                  'q': _search.text.trim(),
                  'updates': '$_updates',
                  'page': '$page',
                },
              )
              as Map;
      if (!mounted || revision != _request) return;
      setState(() {
        final incoming = (data['items'] as List).cast<Map<String, dynamic>>();
        _rows = {
          for (final row in [
            ...(append ? _rows : <Map<String, dynamic>>[]),
            ...incoming,
          ])
            row['id']: row,
        }.values.toList();
        _page = page;
        _total = (data['total'] as num).toInt();
        _loading = false;
        final sync = data['sync'] as Map;
        _message =
            sync['last_error'] as String? ??
            ((sync['pending_tasks'] as num? ?? 0) > 0
                ? '系列检查正在排队，可下拉刷新进度'
                : null);
      });
    } catch (error) {
      if (mounted && revision == _request) {
        setState(() {
          _failure = _error(error);
          _loading = false;
        });
      }
    }
  }

  Future<void> _sync() async {
    setState(() {
      _busy = true;
      _failure = null;
    });
    try {
      await widget.api.post('/pixiv/watchlist/sync');
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(const SnackBar(content: Text('追更同步已加入检查队列')));
        await _load();
      }
    } catch (error) {
      if (mounted) setState(() => _failure = _error(error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _open(Map<String, dynamic> row) {
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => _WatchlistDetail(
          api: widget.api,
          entry: row,
          onOpenItem: widget.onOpenItem,
          onRead: () {
            if (mounted) setState(() => row['has_update'] = false);
          },
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('连载追更'),
      actions: [
        IconButton(
          onPressed: _busy ? null : _sync,
          tooltip: '同步主账号追更',
          icon: const Icon(Icons.sync),
        ),
      ],
    ),
    body: Column(
      children: [
        Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            children: [
              TextField(
                controller: _search,
                decoration: const InputDecoration(
                  labelText: '搜索系列或作者',
                  prefixIcon: Icon(Icons.search),
                ),
                onChanged: (_) {
                  _debounce?.cancel();
                  _debounce = Timer(
                    const Duration(milliseconds: 350),
                    () => _load(),
                  );
                },
              ),
              const SizedBox(height: 12),
              Row(
                children: [
                  Expanded(
                    child: SegmentedButton<String>(
                      segments: const [
                        ButtonSegment(value: 'novel', label: Text('小说')),
                        ButtonSegment(value: 'manga', label: Text('漫画')),
                      ],
                      selected: {_kind},
                      onSelectionChanged: (value) {
                        setState(() => _kind = value.first);
                        _load();
                      },
                    ),
                  ),
                  const SizedBox(width: 12),
                  FilterChip(
                    label: const Text('未读更新'),
                    selected: _updates,
                    onSelected: (value) {
                      setState(() => _updates = value);
                      _load();
                    },
                  ),
                ],
              ),
            ],
          ),
        ),
        if (_message != null)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            child: Text(_message!),
          ),
        if (_failure != null)
          Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              children: [
                Text(
                  _failure!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
                TextButton(onPressed: () => _load(), child: const Text('重试')),
              ],
            ),
          ),
        if (_loading) const LinearProgressIndicator(),
        Expanded(
          child: RefreshIndicator(
            onRefresh: () => _load(),
            child: ListView.builder(
              controller: _scroll,
              physics: const AlwaysScrollableScrollPhysics(),
              itemCount: _rows.isEmpty
                  ? 1
                  : _rows.length + (_rows.length < _total ? 1 : 0),
              itemBuilder: (context, index) {
                if (_rows.isEmpty) {
                  return const Padding(
                    padding: EdgeInsets.all(32),
                    child: Text('暂无追更系列，点击右上角同步读取 Pixiv 官方追更列表。'),
                  );
                }
                if (index >= _rows.length) {
                  return Padding(
                    padding: const EdgeInsets.all(16),
                    child: TextButton(
                      onPressed: _loading ? null : () => _load(append: true),
                      child: const Text('加载更多'),
                    ),
                  );
                }
                final row = _rows[index];
                return ListTile(
                  key: ValueKey(row['id']),
                  isThreeLine: true,
                  leading: Icon(
                    _kind == 'novel'
                        ? Icons.menu_book
                        : Icons.collections_bookmark_outlined,
                  ),
                  title: Text(row['title'] as String),
                  subtitle: Text(
                    '${row['author']} · ${row['content_count']} 篇\n${row['last_error'] ?? (row['auto_download'] == true ? '自动下载正文' : '仅封面与资料')}',
                  ),
                  trailing: row['has_update'] == true
                      ? const Badge(label: Text('更新'))
                      : const Icon(Icons.chevron_right),
                  onTap: () => _open(row),
                );
              },
            ),
          ),
        ),
      ],
    ),
  );
}

class _WatchlistDetail extends StatefulWidget {
  final LibraryApi api;
  final Map<String, dynamic> entry;
  final ValueChanged<String> onOpenItem;
  final VoidCallback onRead;
  const _WatchlistDetail({
    required this.api,
    required this.entry,
    required this.onOpenItem,
    required this.onRead,
  });
  @override
  State<_WatchlistDetail> createState() => _WatchlistDetailState();
}

class _WatchlistDetailState extends State<_WatchlistDetail> {
  List<Map<String, dynamic>> _entries = [];
  bool _loading = true, _busy = false;
  String? _failure;
  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() => _loading = true);
    try {
      final data =
          await widget.api.get('/pixiv/watchlist/${widget.entry['id']}') as Map;
      if (mounted) {
        setState(
          () =>
              _entries = ((data['catalog'] as Map?)?['entries'] as List? ?? [])
                  .cast<Map<String, dynamic>>(),
        );
      }
    } catch (error) {
      if (mounted) setState(() => _failure = _error(error));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _action(String action) async {
    setState(() {
      _busy = true;
      _failure = null;
    });
    try {
      await widget.api.post('/pixiv/watchlist/${widget.entry['id']}/$action');
      if (action == 'read') widget.onRead();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(action == 'read' ? '已标为已读' : '目录检查已进入队列，稍后下拉刷新'),
          ),
        );
      }
    } catch (error) {
      if (mounted) setState(() => _failure = _error(error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: Text(widget.entry['title'] as String)),
    body: Column(
      children: [
        Padding(
          padding: const EdgeInsets.all(16),
          child: Wrap(
            spacing: 12,
            children: [
              FilledButton.tonal(
                onPressed: _busy ? null : () => _action('check'),
                child: const Text('检查目录'),
              ),
              OutlinedButton(
                onPressed: _busy ? null : () => _action('read'),
                child: const Text('标为已读'),
              ),
            ],
          ),
        ),
        if (_failure != null)
          Padding(
            padding: const EdgeInsets.all(16),
            child: Text(
              _failure!,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ),
        if (_loading) const LinearProgressIndicator(),
        Expanded(
          child: RefreshIndicator(
            onRefresh: _load,
            child: ListView.builder(
              physics: const AlwaysScrollableScrollPhysics(),
              itemCount: _entries.isEmpty ? 1 : _entries.length,
              itemBuilder: (context, index) {
                if (_entries.isEmpty) {
                  return const Padding(
                    padding: EdgeInsets.all(24),
                    child: Text('目录尚未获取完成，可检查目录后下拉刷新。'),
                  );
                }
                final entry = _entries[index];
                return ListTile(
                  title: Text('${index + 1}. ${entry['title']}'),
                  subtitle: Text(
                    entry['content_state'] == 'ready' ? '已保存，可阅读' : '待下载',
                  ),
                  onTap: () => widget.onOpenItem(entry['item_id'] as String),
                );
              },
            ),
          ),
        ),
      ],
    ),
  );
}
