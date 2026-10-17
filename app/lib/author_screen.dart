import 'dart:async';
import 'package:flutter/material.dart';
import 'api.dart';
import 'api_image.dart';
import 'models.dart';
import 'author_download_controls.dart';

String _failure(Object error) => error is ApiFailure && error.status == 409
    ? '请先在网页「内容来源」登录 Pixiv 账号。'
    : error is ApiFailure
    ? error.message
    : '无法读取作者资料，请重试';

class AuthorsScreen extends StatefulWidget {
  final LibraryApi api;
  final ValueChanged<String> onOpenItem;
  final String initialQuery;
  const AuthorsScreen({
    super.key,
    required this.api,
    required this.onOpenItem,
    this.initialQuery = '',
  });
  @override
  State<AuthorsScreen> createState() => _AuthorsScreenState();
}

class _AuthorsScreenState extends State<AuthorsScreen> {
  final _query = TextEditingController();
  List<Map<String, dynamic>> _authors = [];
  int _page = 1, _total = 0, _request = 0;
  bool _loading = true;
  String? _error;
  @override
  void initState() {
    super.initState();
    _query.text = widget.initialQuery;
    _load();
  }

  @override
  void dispose() {
    _request++;
    _query.dispose();
    super.dispose();
  }

  Future<void> _load({bool reset = false, bool append = false}) async {
    if (append && (_loading || _page * 30 >= _total)) return;
    final targetPage = append ? _page + 1 : 1;
    final request = ++_request;
    setState(() {
      _loading = true;
      _error = null;
      if (!append) {
        _authors = [];
        _page = 1;
        _total = 0;
      }
    });
    try {
      final result =
          await widget.api.get(
                '/pixiv/authors',
                query: {'q': _query.text.trim(), 'page': '$targetPage'},
              )
              as Map;
      if (!mounted || request != _request) return;
      setState(() {
        final incoming = (result['items'] as List).cast<Map<String, dynamic>>();
        _authors = {
          for (final author in [
            ...(append ? _authors : <Map<String, dynamic>>[]),
            ...incoming,
          ])
            author['remote_id']: author,
        }.values.toList();
        _page = targetPage;
        _total = (result['total'] as num).toInt();
        _loading = false;
      });
    } catch (error) {
      if (mounted && request == _request) {
        setState(() {
          _error = _failure(error);
          _loading = false;
        });
      }
    }
  }

  void _open(String target, {String? section}) {
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => AuthorScreen(
          api: widget.api,
          target: target,
          initialSection: section,
          onOpenItem: widget.onOpenItem,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Pixiv 作者与收藏')),
    body: Column(
      children: [
        Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            children: [
              TextField(
                controller: _query,
                onSubmitted: (_) => _load(reset: true),
                decoration: InputDecoration(
                  labelText: '搜索已关注作者',
                  suffixIcon: IconButton(
                    tooltip: '搜索作者',
                    onPressed: () => _load(reset: true),
                    icon: const Icon(Icons.search),
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                children: [
                  TextButton.icon(
                    onPressed: () => _open('me'),
                    icon: const Icon(Icons.person_outline),
                    label: const Text('我的主页'),
                  ),
                  TextButton.icon(
                    onPressed: () => _open('me', section: 'bookmarks'),
                    icon: const Icon(Icons.bookmarks_outlined),
                    label: const Text('我的收藏'),
                  ),
                  TextButton.icon(
                    onPressed: () async {
                      final value = await showDialog<String>(
                        context: context,
                        builder: (_) => const _AuthorLinkDialog(),
                      );
                      if (mounted && value?.isNotEmpty == true) _open(value!);
                    },
                    icon: const Icon(Icons.link),
                    label: const Text('按链接打开'),
                  ),
                ],
              ),
            ],
          ),
        ),
        if (_loading) const LinearProgressIndicator(),
        if (_error != null)
          Padding(padding: const EdgeInsets.all(16), child: Text(_error!)),
        Expanded(
          child: !_loading && _authors.isEmpty
              ? const Center(child: Text('暂无匹配作者，可按链接打开主页。'))
              : NotificationListener<ScrollNotification>(
                  onNotification: (event) {
                    if (event is ScrollUpdateNotification &&
                        event.metrics.extentAfter < 300 &&
                        !_loading &&
                        _error == null) {
                      _load(append: true);
                    }
                    return false;
                  },
                  child: RefreshIndicator(
                    onRefresh: () => _load(reset: true),
                    child: ListView.builder(
                      physics: const AlwaysScrollableScrollPhysics(),
                      itemCount:
                          _authors.length + (_page * 30 < _total ? 1 : 0),
                      itemBuilder: (context, index) {
                        if (index == _authors.length) {
                          return Center(
                            child: TextButton(
                              onPressed: _loading
                                  ? null
                                  : () => _load(append: true),
                              child: Text(_error != null ? '加载失败，重试' : '加载更多'),
                            ),
                          );
                        }
                        final author = _authors[index];
                        return ListTile(
                          leading: const CircleAvatar(
                            child: Icon(Icons.person_outline),
                          ),
                          title: Text(
                            (author['name'] as String?)?.isNotEmpty == true
                                ? author['name'] as String
                                : 'Pixiv 作者',
                          ),
                          subtitle: Text('ID ${author['remote_id']}'),
                          trailing: const Icon(Icons.chevron_right),
                          onTap: () => _open(author['remote_id'] as String),
                        );
                      },
                    ),
                  ),
                ),
        ),
      ],
    ),
  );
}

class _AuthorLinkDialog extends StatefulWidget {
  const _AuthorLinkDialog();
  @override
  State<_AuthorLinkDialog> createState() => _AuthorLinkDialogState();
}

class _AuthorLinkDialogState extends State<_AuthorLinkDialog> {
  final _input = TextEditingController();
  @override
  void dispose() {
    _input.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
    title: const Text('打开 Pixiv 作者'),
    content: TextField(
      controller: _input,
      autofocus: true,
      decoration: const InputDecoration(hintText: '作者 ID、主页或收藏链接'),
    ),
    actions: [
      TextButton(
        onPressed: () => Navigator.pop(context),
        child: const Text('取消'),
      ),
      FilledButton(
        onPressed: () => Navigator.pop(context, _input.text.trim()),
        child: const Text('打开'),
      ),
    ],
  );
}

class AuthorScreen extends StatefulWidget {
  final LibraryApi api;
  final String target;
  final String? initialSection;
  final ValueChanged<String> onOpenItem;
  const AuthorScreen({
    super.key,
    required this.api,
    required this.target,
    required this.onOpenItem,
    this.initialSection,
  });
  @override
  State<AuthorScreen> createState() => _AuthorScreenState();
}

class _AuthorScreenState extends State<AuthorScreen> {
  String? _id, _error, _profileStatus, _feedStatus;
  Map<String, dynamic>? _profile, _feed;
  String _section = 'works', _category = 'illustration', _restrict = 'public';
  int _page = 1, _request = 0;
  final _feedPages = <int, List<dynamic>>{};
  bool _readingFeed = false;
  bool _loading = false;
  Timer? _profileTimer, _feedTimer;
  bool get _pending => ['queued', 'running'].contains(_feedStatus);
  @override
  void initState() {
    super.initState();
    _start();
  }

  @override
  void dispose() {
    _request++;
    _profileTimer?.cancel();
    _feedTimer?.cancel();
    super.dispose();
  }

  Future<void> _start() async {
    _profileTimer?.cancel();
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final result =
          await widget.api.post('/pixiv/authors/open', {
                'url_or_id': widget.target,
              })
              as Map;
      if (!mounted) return;
      _section = widget.initialSection ?? result['section'] as String;
      _category = result['category'] as String;
      _restrict = result['restrict'] as String;
      if (result['remote_id'] != 'me') {
        _id = result['remote_id'] as String;
        await _readProfile();
        await _readFeed(auto: _restrict == 'public');
      }
      if (mounted) _watchProfile((result['job'] as Map)['id'] as String);
    } catch (error) {
      if (mounted) setState(() => _error = _failure(error));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _readProfile() async {
    final data =
        await widget.api.get('/pixiv/authors/$_id') as Map<String, dynamic>;
    if (mounted) setState(() => _profile = data);
  }

  Future<void> _watchProfile(String jobId) async {
    try {
      final state = await widget.api.get('/pixiv/tasks/$jobId') as Map;
      if (!mounted) return;
      setState(() => _profileStatus = state['status'] as String);
      if (['queued', 'running'].contains(state['status'])) {
        _profileTimer = Timer(
          const Duration(seconds: 3),
          () => _watchProfile(jobId),
        );
      } else if (state['status'] == 'completed') {
        _id = state['author_id'] as String;
        await _readProfile();
        await _readFeed(auto: true);
      } else {
        setState(() => _error = state['error'] as String? ?? '作者资料任务未完成，请重试。');
      }
    } catch (error) {
      if (mounted) setState(() => _error = _failure(error));
    }
  }

  Map<String, String> get _filters => {
    'section': _section,
    'category': _category,
    'restrict': _restrict,
  };
  Future<void> _readFeed({bool auto = false, int? nextPage}) async {
    if (_id == null || (nextPage != null && _readingFeed)) return;
    final page = nextPage ?? _page;
    final request = ++_request;
    setState(() {
      _readingFeed = true;
      _error = null;
    });
    try {
      final data =
          await widget.api.get(
                '/pixiv/authors/$_id/feed',
                query: {..._filters, 'page': '$page'},
              )
              as Map<String, dynamic>;
      if (!mounted || request != _request) return;
      if (_feed != null &&
          _feed!['generation'] != data['generation'] &&
          page > 1) {
        _page = 1;
        _feedPages.clear();
        await _readFeed();
        return;
      }
      final job = data['job'] as Map?;
      setState(() {
        if (_feed != null && _feed!['generation'] != data['generation']) {
          _feedPages.clear();
        }
        _feedPages[page] = data['items'] as List;
        final keys = _feedPages.keys.toList()..sort();
        final combined = <String, dynamic>{};
        for (final key in keys) {
          for (final item in _feedPages[key]!) {
            combined[(item as Map)['id'] as String] = item;
          }
        }
        _feed = {...data, 'items': combined.values.toList()};
        _page = page;
        _readingFeed = false;
        _feedStatus = job?['status'] as String?;
        _loading = false;
      });
      if (job != null && ['queued', 'running'].contains(job['status'])) {
        _feedTimer?.cancel();
        _watchFeed(job['id'] as String, request);
      } else if (auto && data['revision'] == 0 && job == null) {
        _readingFeed = false;
        await _queueFeed();
      } else if (job?['status'] == 'failed') {
        setState(() => _error = job?['error'] as String? ?? '目录读取失败，请重试。');
      }
    } catch (error) {
      if (mounted && request == _request) {
        setState(() => _error = _failure(error));
      }
    } finally {
      if (mounted && request == _request) setState(() => _readingFeed = false);
    }
  }

  Future<void> _queueFeed({bool more = false}) async {
    if (_id == null || _pending) return;
    final request = ++_request;
    _feedTimer?.cancel();
    setState(() {
      _loading = true;
      _error = null;
    });
    if (!more) {
      _page = 1;
      _feedPages.clear();
    }
    try {
      final result =
          await widget.api.post('/pixiv/authors/$_id/feed', {
                ..._filters,
                'more': more,
              })
              as Map;
      if (!mounted || request != _request) return;
      final job = result['job'] as Map?;
      if (job == null) {
        await _readFeed();
      } else {
        setState(() => _feedStatus = job['status'] as String);
        await _watchFeed(job['id'] as String, request);
      }
    } catch (error) {
      if (mounted && request == _request) {
        setState(() => _error = _failure(error));
      }
    } finally {
      if (mounted && request == _request) setState(() => _loading = false);
    }
  }

  Future<void> _watchFeed(String jobId, int request) async {
    try {
      final state = await widget.api.get('/pixiv/tasks/$jobId') as Map;
      if (!mounted || request != _request) return;
      setState(() => _feedStatus = state['status'] as String);
      if (['queued', 'running'].contains(state['status'])) {
        _feedTimer = Timer(
          const Duration(seconds: 3),
          () => _watchFeed(jobId, request),
        );
      } else {
        await _readFeed();
        if (mounted && state['status'] == 'failed') {
          setState(() => _error = state['error'] as String? ?? '目录读取失败，请重试。');
        }
      }
    } catch (error) {
      if (mounted && request == _request) {
        setState(() => _error = _failure(error));
      }
    }
  }

  void _choose({String? section, String? category, String? restrict}) {
    _request++;
    _feedTimer?.cancel();
    setState(() {
      _section = section ?? _section;
      _category = category ?? _category;
      _restrict = _section == 'works' ? 'public' : restrict ?? _restrict;
      _page = 1;
      _feedPages.clear();
      _readingFeed = false;
      _feed = null;
      _feedStatus = null;
      _error = null;
      _loading = false;
    });
    _readFeed(auto: true);
  }

  Future<void> _refreshFeed() async {
    if (_pending || _loading) return;
    _page = 1;
    _feedPages.clear();
    await _readFeed();
  }

  Future<void> _loadMoreFeed() async {
    if (_readingFeed || _loading || _pending || _id == null) return;
    final total = (_feed?['total'] as num? ?? 0).toInt();
    if (_page * 24 < total) {
      await _readFeed(nextPage: _page + 1);
    } else if (_feed?['has_more'] == true) {
      await _queueFeed(more: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final items = (_feed?['items'] as List? ?? [])
        .map((v) => LibraryItem.fromJson(v as Map<String, dynamic>))
        .toList();
    final total = (_feed?['total'] as num? ?? 0).toInt();
    return Scaffold(
      appBar: AppBar(
        title: Text(
          _profile?['name'] as String? ?? 'Pixiv 作者主页',
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
        ),
        actions: [
          IconButton(
            tooltip: '刷新作者资料',
            onPressed: _loading ? null : _start,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (_profile != null)
                  Row(
                    children: [
                      SizedBox(
                        width: 52,
                        height: 52,
                        child: ClipOval(
                          child: _profile!['avatar_url'] == null
                              ? const CircleAvatar(
                                  child: Icon(Icons.person_outline),
                                )
                              : ApiImage(
                                  api: widget.api,
                                  path: _profile!['avatar_url'] as String,
                                ),
                        ),
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              _profile!['name'] as String,
                              style: Theme.of(context).textTheme.titleMedium,
                            ),
                            Text(
                              'Pixiv ID $_id',
                              style: Theme.of(context).textTheme.bodySmall,
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                if ((_profile?['description'] as String? ?? '').isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Text(
                      _profile!['description'] as String,
                      maxLines: 3,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                if (_id != null && _profile?['download_settings'] is Map)
                  AuthorDownloadControls(
                    api: widget.api,
                    authorId: _id!,
                    initial: Map<String, dynamic>.from(
                      _profile!['download_settings'] as Map,
                    ),
                  ),
                const SizedBox(height: 10),
                SegmentedButton<String>(
                  segments: const [
                    ButtonSegment(value: 'works', label: Text('作者作品')),
                    ButtonSegment(value: 'bookmarks', label: Text('收藏')),
                  ],
                  selected: {_section},
                  onSelectionChanged: (v) => _choose(section: v.first),
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 8,
                  children: [
                    for (final entry in {
                      'illustration': '插画',
                      'manga': '漫画',
                      'novel': '小说',
                    }.entries)
                      ChoiceChip(
                        label: Text(entry.value),
                        selected: _category == entry.key,
                        onSelected: (_) => _choose(category: entry.key),
                      ),
                    if (_section == 'bookmarks' &&
                        _profile?['can_read_private'] == true)
                      ChoiceChip(
                        label: const Text('私密收藏'),
                        selected: _restrict == 'private',
                        onSelected: (v) =>
                            _choose(restrict: v ? 'private' : 'public'),
                      ),
                  ],
                ),
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        '已读取 $total 部${_feed?['complete'] == true ? ' · 目录已完整读取' : ''}',
                      ),
                    ),
                    TextButton(
                      onPressed: _id == null || _pending || _loading
                          ? null
                          : () => _queueFeed(),
                      child: const Text('重新读取目录'),
                    ),
                  ],
                ),
                if (_pending || ['queued', 'running'].contains(_profileStatus))
                  const Text(
                    '正在服务器队列中读取；采集暂停时，请在网页任务页恢复。',
                    style: TextStyle(fontSize: 13),
                  ),
                if ((_feed?['unavailable'] as num? ?? 0) > 0)
                  const Text(
                    '含不可访问或暂不支持的条目，目录可能不完整。',
                    style: TextStyle(fontSize: 13),
                  ),
              ],
            ),
          ),
          if (_loading) const LinearProgressIndicator(),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              child: Text(
                _error!,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
            ),
          Expanded(
            child: items.isEmpty
                ? const Center(child: Text('目录尚无可显示内容'))
                : NotificationListener<ScrollNotification>(
                    onNotification: (event) {
                      if (event is ScrollUpdateNotification &&
                          event.metrics.extentAfter < 300 &&
                          _error == null) {
                        _loadMoreFeed();
                      }
                      return false;
                    },
                    child: RefreshIndicator(
                      onRefresh: _refreshFeed,
                      child: ListView.builder(
                        physics: const AlwaysScrollableScrollPhysics(),
                        itemCount:
                            items.length +
                            ((_page * 24 < total ||
                                    _feed?['has_more'] == true ||
                                    _pending ||
                                    _readingFeed)
                                ? 1
                                : 0),
                        itemBuilder: (context, index) {
                          if (index == items.length) {
                            return Padding(
                              padding: const EdgeInsets.all(16),
                              child: _pending || _readingFeed
                                  ? const Center(
                                      child: SizedBox(
                                        width: 20,
                                        height: 20,
                                        child: CircularProgressIndicator(
                                          strokeWidth: 2,
                                        ),
                                      ),
                                    )
                                  : Center(
                                      child: TextButton(
                                        onPressed: _loading
                                            ? null
                                            : _loadMoreFeed,
                                        child: Text(
                                          _error != null ? '加载失败，重试' : '加载更多',
                                        ),
                                      ),
                                    ),
                            );
                          }
                          final item = items[index];
                          return ListTile(
                            leading: SizedBox(
                              width: 44,
                              height: 58,
                              child: item.coverUrl == null
                                  ? const Icon(Icons.menu_book_outlined)
                                  : ApiImage(
                                      api: widget.api,
                                      path: item.coverUrl!,
                                    ),
                            ),
                            title: Text(
                              item.displayTitle,
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                            ),
                            subtitle: Text(
                              '${item.author} · ${item.readable ? '可阅读' : '正文未下载'}',
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                            ),
                            trailing: const Icon(Icons.chevron_right),
                            onTap: () => widget.onOpenItem(item.id),
                          );
                        },
                      ),
                    ),
                  ),
          ),
        ],
      ),
    );
  }
}
