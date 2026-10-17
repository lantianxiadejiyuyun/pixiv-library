import 'publication_time.dart';
import 'dart:async';

import 'package:flutter/material.dart';

import 'api.dart';
import 'novel_reader_body.dart';
import 'pixiv_direct_api.dart';
import 'reader_preferences.dart';

String _error(Object error) =>
    error is ApiFailure ? error.message : 'Pixiv 读取失败，请重试';
String? _cover(Map<String, dynamic> work) {
  final images = work['image_urls'];
  return images is Map
      ? (images['medium'] ?? images['large'] ?? images['square_medium'])
            as String?
      : null;
}

class PixivDirectScreen extends StatefulWidget {
  final LibraryApi library;
  final PixivDirectApi? direct;
  final String? initialId;
  final bool initialNovel;
  const PixivDirectScreen({
    super.key,
    required this.library,
    this.direct,
    this.initialId,
    this.initialNovel = false,
  });
  @override
  State<PixivDirectScreen> createState() => _PixivDirectScreenState();
}

class _PixivDirectScreenState extends State<PixivDirectScreen> {
  late final PixivDirectApi _api =
      widget.direct ??
      PixivDirectApi(
        store: SecurePixivTokenStore(widget.library.server),
        scope: widget.library.server.origin,
      );
  final _search = TextEditingController();
  final _scroll = ScrollController();
  final _seen = <String>{};
  List<Map<String, dynamic>> _works = [];
  String _mode = 'bookmarks', _category = 'illustration', _restrict = 'public';
  String? _next, _failure;
  bool _loading = true, _importing = false;
  int _request = 0;

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.hasClients &&
          _scroll.position.extentAfter < 400 &&
          !_loading &&
          _failure == null &&
          _next != null) {
        _load(more: true);
      }
    });
    _restore();
  }

  Future<void> _restore() async {
    try {
      await _api.restore();
      if (!mounted) return;
      if (_api.connected) {
        if (widget.initialId != null) {
          WidgetsBinding.instance.addPostFrameCallback((_) {
            if (mounted) _open(widget.initialId!, widget.initialNovel);
          });
        }
        await _load();
      } else {
        setState(() => _loading = false);
      }
    } catch (error) {
      if (mounted) {
        setState(() {
          _loading = false;
          _failure = _error(error);
        });
      }
    }
  }

  Future<void> _import() async {
    final password = await showDialog<String>(
      context: context,
      builder: (_) => const _ImportDialog(),
    );
    if (!mounted || password == null) return;
    _request++;
    setState(() {
      _importing = true;
      _failure = null;
    });
    try {
      final result =
          await widget.library.post('/pixiv/direct/credentials', {
                'admin_password': password,
              })
              as Map;
      if (!mounted) return;
      await _api.connect(result['refresh_token'] as String);
      if (!mounted) return;
      setState(() {
        _works = [];
      });
      if (widget.initialId != null) {
        _open(widget.initialId!, widget.initialNovel);
      }
      await _load();
    } catch (error) {
      if (mounted) {
        setState(() => _failure = _error(error));
      }
    } finally {
      if (mounted) setState(() => _importing = false);
    }
  }

  Future<void> _load({bool more = false}) async {
    if (!_api.connected || (more && (_loading || _next == null))) return;
    final request = ++_request;
    final cursor = more ? _next : null;
    setState(() {
      _loading = true;
      _failure = null;
    });
    try {
      final page = await _api.page(
        mode: _mode,
        category: _category,
        restrict: _restrict,
        query: _search.text.trim(),
        next: cursor,
      );
      if (!mounted || request != _request) return;
      if (more && cursor != null && !_seen.add(cursor)) {
        throw const ApiFailure('Pixiv 分页重复，已停止加载');
      }
      if (more &&
          page.next != null &&
          (_seen.contains(page.next) || page.next == cursor)) {
        throw const ApiFailure('Pixiv 分页重复，已停止加载');
      }
      setState(() {
        if (!more) _seen.clear();
        _works = {
          for (final value in [
            ...(more ? _works : <Map<String, dynamic>>[]),
            ...page.works,
          ])
            '${value['_novel']}:${value['id']}': value,
        }.values.toList();
        _next = page.next;
        _loading = false;
      });
    } catch (error) {
      if (mounted && request == _request) {
        setState(() {
          _failure = _error(error);
          _loading = false;
        });
      }
    }
  }

  void _filter(VoidCallback change) {
    _request++;
    setState(() {
      change();
      _works = [];
      _next = null;
      _seen.clear();
      _loading = false;
      _failure = null;
    });
    if (_scroll.hasClients) _scroll.jumpTo(0);
    _load();
  }

  Future<void> _logout() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('清除本机 Pixiv 令牌？'),
        content: const Text('本机保存的 Pixiv 令牌和图片缓存将被清除。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('清除'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    _request++;
    setState(() {
      _importing = true;
    });
    try {
      await _api.logout();
    } catch (error) {
      if (mounted) setState(() => _failure = _error(error));
    } finally {
      if (mounted) {
        setState(() {
          _works = [];
          _next = null;
          _loading = false;
          _importing = false;
        });
      }
    }
  }

  void _open(String ident, bool novel) => Navigator.of(context).push(
    MaterialPageRoute<void>(
      builder: (_) => PixivOnlineWorkScreen(api: _api, id: ident, novel: novel),
    ),
  );

  @override
  void dispose() {
    _request++;
    _api.close();
    _search.dispose();
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('Pixiv 在线'),
      actions: [
        PopupMenuButton<String>(
          onSelected: _importing
              ? null
              : (value) => value == 'import' ? _import() : _logout(),
          itemBuilder: (_) => [
            const PopupMenuItem(value: 'import', child: Text('导入主账号令牌')),
            if (_api.connected)
              const PopupMenuItem(value: 'logout', child: Text('清除本机令牌')),
          ],
        ),
      ],
    ),
    body: !_api.connected
        ? Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.public, size: 48),
                  const SizedBox(height: 16),
                  const Text(
                    '手机直接访问 Pixiv，使用主账号查看作品和收藏。令牌保存在手机安全存储中。',
                    textAlign: TextAlign.center,
                  ),
                  const SizedBox(height: 16),
                  FilledButton.icon(
                    onPressed: _importing ? null : _import,
                    icon: const Icon(Icons.key),
                    label: const Text('导入主账号令牌'),
                  ),
                  if (_importing || _loading)
                    const Padding(
                      padding: EdgeInsets.all(16),
                      child: LinearProgressIndicator(),
                    ),
                  if (_failure != null)
                    Text(
                      _failure!,
                      style: TextStyle(
                        color: Theme.of(context).colorScheme.error,
                      ),
                    ),
                ],
              ),
            ),
          )
        : Column(
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '${_api.userName} · 手机直连',
                      style: Theme.of(context).textTheme.labelLarge,
                    ),
                    const SizedBox(height: 8),
                    Wrap(
                      spacing: 8,
                      children: [
                        for (final mode in const {
                          'bookmarks': '我的收藏',
                          'following': '关注新作',
                          'search': '搜索',
                        }.entries)
                          ChoiceChip(
                            label: Text(mode.value),
                            selected: _mode == mode.key,
                            onSelected: (_) {
                              if (mode.key == 'search' &&
                                  _search.text.trim().isEmpty) {
                                _request++;
                                setState(() {
                                  _mode = 'search';
                                  _works = [];
                                  _next = null;
                                  _failure = null;
                                  _loading = false;
                                });
                              } else {
                                _filter(() => _mode = mode.key);
                              }
                            },
                          ),
                      ],
                    ),
                    if (_mode == 'search')
                      TextField(
                        controller: _search,
                        textInputAction: TextInputAction.search,
                        onSubmitted: (_) => _filter(() {}),
                        decoration: InputDecoration(
                          labelText: '搜索 Pixiv 作品',
                          suffixIcon: IconButton(
                            onPressed: () => _filter(() {}),
                            icon: const Icon(Icons.search),
                          ),
                        ),
                      ),
                    Row(
                      children: [
                        Expanded(
                          child: DropdownButton<String>(
                            isExpanded: true,
                            value: _category,
                            items: const [
                              DropdownMenuItem(
                                value: 'illustration',
                                child: Text('插画'),
                              ),
                              DropdownMenuItem(
                                value: 'manga',
                                child: Text('漫画'),
                              ),
                              DropdownMenuItem(
                                value: 'novel',
                                child: Text('小说'),
                              ),
                            ],
                            onChanged: (value) {
                              if (value != null) {
                                _filter(() => _category = value);
                              }
                            },
                          ),
                        ),
                        if (_mode == 'bookmarks') ...[
                          const SizedBox(width: 16),
                          DropdownButton<String>(
                            value: _restrict,
                            items: const [
                              DropdownMenuItem(
                                value: 'public',
                                child: Text('公开收藏'),
                              ),
                              DropdownMenuItem(
                                value: 'private',
                                child: Text('私密收藏'),
                              ),
                            ],
                            onChanged: (value) {
                              if (value != null) {
                                _filter(() => _restrict = value);
                              }
                            },
                          ),
                        ],
                      ],
                    ),
                  ],
                ),
              ),
              if (_importing || (_loading && _works.isEmpty))
                const LinearProgressIndicator(),
              Expanded(
                child: RefreshIndicator(
                  onRefresh: () => _load(),
                  child: ListView.builder(
                    controller: _scroll,
                    physics: const AlwaysScrollableScrollPhysics(),
                    itemCount: _works.length + 1,
                    itemBuilder: (context, index) {
                      if (index == _works.length) {
                        return Padding(
                          padding: const EdgeInsets.all(24),
                          child: Column(
                            children: [
                              if (_failure != null) ...[
                                Text(_failure!),
                                TextButton(
                                  onPressed: () => _load(
                                    more: _works.isNotEmpty && _next != null,
                                  ),
                                  child: const Text('重试'),
                                ),
                              ] else if (_loading)
                                const CircularProgressIndicator()
                              else if (_next != null)
                                TextButton(
                                  onPressed: () => _load(more: true),
                                  child: const Text('加载更多'),
                                )
                              else
                                Text(
                                  _works.isEmpty
                                      ? (_mode == 'search' ? '输入关键词搜索' : '暂无作品')
                                      : '已到末尾',
                                ),
                            ],
                          ),
                        );
                      }
                      final work = _works[index];
                      final cover = _cover(work);
                      final user = work['user'];
                      return ListTile(
                        contentPadding: const EdgeInsets.symmetric(
                          horizontal: 16,
                          vertical: 6,
                        ),
                        leading: SizedBox(
                          width: 64,
                          height: 80,
                          child: cover == null
                              ? const Icon(Icons.book_outlined)
                              : PixivDirectImage(api: _api, url: cover),
                        ),
                        title: Text(
                          '${work['title']}',
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                        ),
                        subtitle: Text(
                          '${user is Map ? user['name'] ?? '' : ''}\n${work['page_count'] ?? 1} P · ★ ${work['total_bookmarks'] ?? 0}',
                        ),
                        isThreeLine: true,
                        onTap: () =>
                            _open('${work['id']}', work['_novel'] == true),
                      );
                    },
                  ),
                ),
              ),
            ],
          ),
  );
}

class _ImportDialog extends StatefulWidget {
  const _ImportDialog();
  @override
  State<_ImportDialog> createState() => _ImportDialogState();
}

class _ImportDialogState extends State<_ImportDialog> {
  final _password = TextEditingController();
  @override
  void dispose() {
    _password.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
    title: const Text('导入主账号令牌'),
    content: Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        const Text('验证服务器管理员密码后，将当前 Pixiv 主账号令牌保存到本机。'),
        const SizedBox(height: 16),
        TextField(
          controller: _password,
          obscureText: true,
          enableSuggestions: false,
          autocorrect: false,
          decoration: const InputDecoration(labelText: '服务器管理员密码'),
          onSubmitted: (_) => Navigator.pop(context, _password.text),
        ),
      ],
    ),
    actions: [
      TextButton(
        onPressed: () => Navigator.pop(context),
        child: const Text('取消'),
      ),
      FilledButton(
        onPressed: () => Navigator.pop(context, _password.text),
        child: const Text('导入'),
      ),
    ],
  );
}

class PixivDirectImage extends StatefulWidget {
  final PixivDirectApi api;
  final String url;
  final BoxFit fit;
  const PixivDirectImage({
    super.key,
    required this.api,
    required this.url,
    this.fit = BoxFit.cover,
  });
  @override
  State<PixivDirectImage> createState() => _PixivDirectImageState();
}

class _PixivDirectImageState extends State<PixivDirectImage> {
  late Future<dynamic> _bytes;
  @override
  void initState() {
    super.initState();
    _bytes = widget.api.image(widget.url);
  }

  @override
  void didUpdateWidget(PixivDirectImage old) {
    super.didUpdateWidget(old);
    if (old.api != widget.api || old.url != widget.url) {
      _bytes = widget.api.image(widget.url);
    }
  }

  void _retry() => setState(() {
    _bytes = (() async {
      await widget.api.evictImage(widget.url);
      return widget.api.image(widget.url);
    })();
  });
  Widget _failed() => Center(
    child: IconButton(
      onPressed: _retry,
      tooltip: '重试当前图片',
      icon: const Icon(Icons.refresh),
    ),
  );
  @override
  Widget build(BuildContext context) => FutureBuilder<dynamic>(
    future: _bytes,
    builder: (context, result) {
      if (result.hasError) return _failed();
      if (!result.hasData) {
        return const Center(child: CircularProgressIndicator(strokeWidth: 2));
      }
      return Image.memory(
        result.data,
        fit: widget.fit,
        gaplessPlayback: true,
        errorBuilder: (_, error, stack) => _failed(),
      );
    },
  );
}

class PixivOnlineWorkScreen extends StatefulWidget {
  final PixivDirectApi api;
  final String id;
  final bool novel;
  const PixivOnlineWorkScreen({
    super.key,
    required this.api,
    required this.id,
    required this.novel,
  });
  @override
  State<PixivOnlineWorkScreen> createState() => _PixivOnlineWorkScreenState();
}

class _PixivOnlineWorkScreenState extends State<PixivOnlineWorkScreen> {
  Map<String, dynamic>? _work;
  String? _failure, _text;
  bool _loading = true;
  final _scroll = ScrollController();
  final _preferences = const ReaderPreferences();
  double _font = 19;
  @override
  void initState() {
    super.initState();
    _load();
    _fontSize();
  }

  Future<void> _fontSize() async {
    try {
      final value = await _preferences.fontSize();
      if (mounted) setState(() => _font = value);
    } catch (_) {
      /* Optional preferences. */
    }
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _failure = null;
    });
    try {
      final work = await widget.api.detail(widget.id, novel: widget.novel);
      final text = widget.novel ? await widget.api.novelText(widget.id) : null;
      if (mounted) {
        setState(() {
          _work = work;
          _text = text;
          _loading = false;
        });
      }
    } catch (error) {
      if (mounted) {
        setState(() {
          _failure = _error(error);
          _loading = false;
        });
      }
    }
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final work = _work;
    return Scaffold(
      appBar: AppBar(
        title: Text(work?['title'] as String? ?? 'Pixiv 在线作品'),
        bottom: work == null
            ? null
            : PreferredSize(
                preferredSize: const Size.fromHeight(36),
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
                  child: Align(
                    alignment: Alignment.centerLeft,
                    child: Text(
                      '原作者发布时间：${publicationTime(parsePublicationTime(work['create_date']))}',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                  ),
                ),
              ),
        actions: [
          if (widget.novel)
            IconButton(
              tooltip: '小说字号',
              icon: const Icon(Icons.text_fields),
              onPressed: () {
                setState(() => _font = _font >= 29 ? 16 : _font + 2);
                unawaited(
                  _preferences.saveFontSize(_font).catchError((Object _) {}),
                );
              },
            ),
        ],
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _failure != null
          ? Center(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(_failure!),
                    TextButton(onPressed: _load, child: const Text('重试')),
                  ],
                ),
              ),
            )
          : work == null
          ? const SizedBox.shrink()
          : widget.novel
          ? SelectionArea(
              child: NovelReaderBody(
                text: _text!,
                title: '${work['title']}',
                fontSize: _font,
                restoreOffset: 0,
                controller: _scroll,
                footer: const Padding(
                  padding: EdgeInsets.all(24),
                  child: Text('已到末尾'),
                ),
                onLayoutStart: () {},
                onLayoutReady: () {},
              ),
            )
          : ListView(
              padding: const EdgeInsets.all(16),
              children: [
                if (_cover(work) != null)
                  SizedBox(
                    height: 260,
                    child: PixivDirectImage(
                      api: widget.api,
                      url: _cover(work)!,
                      fit: BoxFit.contain,
                    ),
                  ),
                const SizedBox(height: 16),
                Text(
                  '${work['title']}',
                  style: Theme.of(context).textTheme.headlineSmall,
                ),
                const SizedBox(height: 12),
                Text(
                  '${work['user'] is Map ? (work['user'] as Map)['name'] ?? '' : ''} · ${work['page_count'] ?? 1} P · ★ ${work['total_bookmarks'] ?? 0}',
                ),
                const SizedBox(height: 16),
                Wrap(
                  spacing: 8,
                  runSpacing: 6,
                  children: [
                    for (final tag in (work['tags'] as List? ?? []))
                      if (tag is Map)
                        Chip(
                          label: Text(
                            '${tag['translated_name'] ?? tag['name']}',
                          ),
                        ),
                  ],
                ),
                const SizedBox(height: 20),
                FilledButton.icon(
                  icon: const Icon(Icons.chrome_reader_mode_outlined),
                  label: const Text('在线阅读'),
                  onPressed: () {
                    try {
                      final pages = PixivDirectApi.pages(work);
                      Navigator.push(
                        context,
                        MaterialPageRoute<void>(
                          builder: (_) => _OnlineComic(
                            api: widget.api,
                            pages: pages,
                            title: '${work['title']}',
                          ),
                        ),
                      );
                    } catch (error) {
                      ScaffoldMessenger.of(
                        context,
                      ).showSnackBar(SnackBar(content: Text(_error(error))));
                    }
                  },
                ),
              ],
            ),
    );
  }
}

class _OnlineComic extends StatefulWidget {
  final PixivDirectApi api;
  final List<String> pages;
  final String title;
  const _OnlineComic({
    required this.api,
    required this.pages,
    required this.title,
  });
  @override
  State<_OnlineComic> createState() => _OnlineComicState();
}

class _OnlineComicState extends State<_OnlineComic> {
  int _page = 0;
  bool _tools = true;
  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: Colors.black,
    appBar: _tools
        ? AppBar(
            title: Text(widget.title),
            backgroundColor: Colors.black,
            foregroundColor: Colors.white,
          )
        : null,
    body: Stack(
      children: [
        PageView.builder(
          itemCount: widget.pages.length,
          onPageChanged: (value) => setState(() => _page = value),
          itemBuilder: (_, index) => _OnlinePage(
            key: ValueKey(widget.pages[index]),
            api: widget.api,
            url: widget.pages[index],
            onTap: () => setState(() => _tools = !_tools),
          ),
        ),
        Positioned(
          bottom: 16,
          left: 0,
          right: 0,
          child: IgnorePointer(
            child: Center(
              child: DecoratedBox(
                decoration: BoxDecoration(
                  color: Colors.black54,
                  borderRadius: BorderRadius.circular(16),
                ),
                child: Padding(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 12,
                    vertical: 5,
                  ),
                  child: Text(
                    '${_page + 1} / ${widget.pages.length}',
                    style: const TextStyle(color: Colors.white),
                  ),
                ),
              ),
            ),
          ),
        ),
      ],
    ),
  );
}

class _OnlinePage extends StatefulWidget {
  final PixivDirectApi api;
  final String url;
  final VoidCallback onTap;
  const _OnlinePage({
    super.key,
    required this.api,
    required this.url,
    required this.onTap,
  });
  @override
  State<_OnlinePage> createState() => _OnlinePageState();
}

class _OnlinePageState extends State<_OnlinePage> {
  final _transform = TransformationController();
  @override
  void dispose() {
    _transform.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => GestureDetector(
    onTap: widget.onTap,
    onDoubleTap: () {
      _transform.value = _transform.value.getMaxScaleOnAxis() > 1
          ? Matrix4.identity()
          : (Matrix4.identity()..scaleByDouble(2, 2, 1, 1));
    },
    child: InteractiveViewer(
      transformationController: _transform,
      minScale: 1,
      maxScale: 5,
      child: SizedBox.expand(
        child: PixivDirectImage(
          api: widget.api,
          url: widget.url,
          fit: BoxFit.contain,
        ),
      ),
    ),
  );
}
