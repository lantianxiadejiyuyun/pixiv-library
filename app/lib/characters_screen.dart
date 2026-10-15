import 'dart:async';
import 'dart:ui';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'api.dart';
import 'api_image.dart';
import 'models.dart';

const _sources = {
  '': '全部书库',
  'pixiv': 'Pixiv',
  'ehentai': 'E-Hentai',
  'picacg': '哔咔',
  'jm': 'JM 天堂',
  'local': '本地书库',
};
String _message(Object e) => e is ApiFailure ? e.message : '无法读取人物资料，请重试';
String _sourceURL(dynamic value) =>
    value is String &&
        RegExp(
          r'^https://anilist\.co/(character|manga|anime)/[1-9]\d*$',
        ).hasMatch(value)
    ? value
    : '';

class CharactersScreen extends StatefulWidget {
  final LibraryApi api;
  final Future<void> Function(String) onOpenItem;
  final WallCharacter? character;
  final String initialSource;
  final bool initialDownloaded;
  final ValueChanged<WallCharacter>? onCharacterChanged;
  const CharactersScreen({
    super.key,
    required this.api,
    required this.onOpenItem,
    this.character,
    this.initialSource = '',
    this.initialDownloaded = false,
    this.onCharacterChanged,
  });
  @override
  State<CharactersScreen> createState() => _CharactersScreenState();
}

class _CharactersScreenState extends State<CharactersScreen> {
  final _search = TextEditingController(), _scroll = ScrollController();
  Timer? _debounce;
  List<WallCharacter> _people = [];
  List<LibraryItem> _works = [];
  WallCharacter? _character;
  String _query = '', _source = '';
  String? _error;
  bool _downloaded = false, _loading = true, _failedMore = false, _blur = true;
  int _request = 0, _page = 1, _total = 0;
  bool get _detail => widget.character != null;
  bool get _more => (_detail ? _works.length : _people.length) < _total;
  @override
  void initState() {
    super.initState();
    _character = widget.character;
    _source = widget.initialSource;
    _downloaded = widget.initialDownloaded;
    _scroll.addListener(() {
      if (_scroll.hasClients &&
          _scroll.position.extentAfter < 350 &&
          !_loading &&
          _error == null &&
          _more) {
        _load(append: true);
      }
    });
    _load();
  }

  @override
  void dispose() {
    _request++;
    _debounce?.cancel();
    _search.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _load({bool append = false}) async {
    if (append && (_loading || !_more)) return;
    final request = ++_request, next = append ? _page + 1 : 1;
    final filters = {
      if (_source.isNotEmpty) 'source': _source,
      'downloaded': '$_downloaded',
    };
    setState(() {
      _loading = true;
      _error = null;
      _failedMore = false;
      if (!append) {
        _page = 1;
        _total = 0;
        _people = [];
        _works = [];
      }
    });
    try {
      final result = await Future.wait([
        widget.api.get(
          _detail ? '/items' : '/characters',
          query: {
            ...filters,
            'page': '$next',
            'page_size': '24',
            if (_detail) 'tag': _character!.id else 'q': _query,
          },
        ),
        if (_detail && !append)
          widget.api.get(
            '/characters/${Uri.encodeComponent(_character!.id)}',
            query: filters,
          ),
      ]);
      if (!mounted || request != _request) return;
      final data = result[0] as Map;
      setState(() {
        if (_detail) {
          final incoming = (data['items'] as List).map(
            (v) => LibraryItem.fromJson(Map<String, dynamic>.from(v as Map)),
          );
          _works = {
            for (final item in [
              ...(append ? _works : <LibraryItem>[]),
              ...incoming,
            ])
              item.id: item,
          }.values.toList();
          if (result.length > 1) {
            _character = WallCharacter.fromJson(
              Map<String, dynamic>.from(result[1] as Map),
            );
          }
        } else {
          final incoming = (data['items'] as List).map(
            (v) => WallCharacter.fromJson(Map<String, dynamic>.from(v as Map)),
          );
          _people = {
            for (final item in [
              ...(append ? _people : <WallCharacter>[]),
              ...incoming,
            ])
              item.id: item,
          }.values.toList();
        }
        _page = next;
        _total = (data['total'] as num).toInt();
        _loading = false;
      });
    } catch (e) {
      if (mounted && request == _request) {
        setState(() {
          _error = _message(e);
          _loading = false;
          _failedMore = append;
        });
      }
    }
  }

  void _changed(String value) {
    _request++;
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 350), () {
      _query = value.trim();
      _load();
    });
  }

  Future<void> _open(WallCharacter character) async {
    await Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => CharactersScreen(
          api: widget.api,
          onOpenItem: widget.onOpenItem,
          character: character,
          initialSource: _source,
          initialDownloaded: _downloaded,
          onCharacterChanged: (updated) {
            if (!mounted) return;
            setState(
              () => _people = [
                for (final person in _people)
                  if (person.id == updated.id) updated else person,
              ],
            );
            widget.onCharacterChanged?.call(updated);
          },
        ),
      ),
    );
  }

  Future<void> _lookup() async {
    final result = await Navigator.of(context).push<WallCharacter>(
      MaterialPageRoute(
        builder: (_) => CharacterResearchScreen(
          api: widget.api,
          character: _detail ? _character : null,
        ),
      ),
    );
    if (!mounted || result == null) return;
    if (_detail) {
      setState(() => _character = result);
      widget.onCharacterChanged?.call(result);
    } else {
      await _open(result);
    }
  }

  Widget _cover(String? path, String rating) {
    Widget image = path == null
        ? const Center(child: Icon(Icons.person_outline, size: 44))
        : ApiImage(api: widget.api, path: path);
    if (_blur && rating == 'adult') {
      image = ImageFiltered(
        imageFilter: ImageFilter.blur(sigmaX: 16, sigmaY: 16),
        child: image,
      );
    }
    return ColoredBox(
      color: Theme.of(context).colorScheme.surfaceContainerHighest,
      child: image,
    );
  }

  Widget _profile() {
    final profile = _character!.profile;
    final originals = profile?['works'] as List? ?? [];
    final aliases = profile?['aliases'] as List? ?? [];
    final url = _sourceURL(profile?['source_url']);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(18),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              _character!.displayName,
              style: Theme.of(context).textTheme.headlineSmall,
            ),
            if (profile?['name'] != null &&
                profile?['name'] != _character!.displayName)
              Text(profile!['name'] as String),
            if (aliases.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(aliases.join(' / ')),
              ),
            const SizedBox(height: 14),
            SelectableText(
              (profile?['description'] as String?)?.isNotEmpty == true
                  ? profile!['description'] as String
                  : profile == null
                  ? '尚未保存人物介绍，可联网搜索并确认对应人物。'
                  : '资料来源尚未提供人物介绍。',
            ),
            if (originals.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Wrap(
                  spacing: 8,
                  runSpacing: 4,
                  children: [
                    for (final work in originals)
                      Chip(label: Text('原作：${(work as Map)['title']}')),
                  ],
                ),
              ),
            if (url.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: SelectableText(
                  '资料来源 · ${profile?['provider']}\n$url',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
            if (url.isNotEmpty)
              TextButton.icon(
                onPressed: () => Clipboard.setData(ClipboardData(text: url)),
                icon: const Icon(Icons.copy, size: 16),
                label: const Text('复制资料来源'),
              ),
            TextButton.icon(
              onPressed: _lookup,
              icon: const Icon(Icons.travel_explore),
              label: Text(profile == null ? '补全人物资料' : '更新人物资料'),
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: Text(
        _detail ? _character!.displayName : '人物作品墙',
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
      ),
      actions: [
        IconButton(
          tooltip: _blur ? '显示 R18 封面' : '模糊 R18 封面',
          onPressed: () => setState(() => _blur = !_blur),
          icon: Icon(
            _blur ? Icons.visibility_off_outlined : Icons.visibility_outlined,
          ),
        ),
        if (!_detail)
          IconButton(
            tooltip: '联网添加人物',
            onPressed: _lookup,
            icon: const Icon(Icons.person_add_alt_1_outlined),
          ),
      ],
    ),
    body: RefreshIndicator(
      onRefresh: () => _load(),
      child: CustomScrollView(
        controller: _scroll,
        physics: const AlwaysScrollableScrollPhysics(),
        slivers: [
          SliverPadding(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
            sliver: SliverToBoxAdapter(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (!_detail)
                    TextField(
                      controller: _search,
                      onChanged: _changed,
                      onSubmitted: (value) {
                        _debounce?.cancel();
                        _query = value.trim();
                        _load();
                      },
                      decoration: const InputDecoration(
                        hintText: '搜索人物、中文名或别名',
                        prefixIcon: Icon(Icons.search),
                      ),
                    ),
                  Row(
                    children: [
                      Expanded(
                        child: DropdownButton<String>(
                          isExpanded: true,
                          value: _source,
                          items: [
                            for (final entry in _sources.entries)
                              DropdownMenuItem(
                                value: entry.key,
                                child: Text(entry.value),
                              ),
                          ],
                          onChanged: (value) {
                            if (value != null) {
                              setState(() => _source = value);
                              _load();
                            }
                          },
                        ),
                      ),
                      const SizedBox(width: 16),
                      FilterChip(
                        label: const Text('仅已下载'),
                        selected: _downloaded,
                        onSelected: (value) {
                          setState(() => _downloaded = value);
                          _load();
                        },
                      ),
                    ],
                  ),
                  if (_detail) _profile(),
                  Padding(
                    padding: const EdgeInsets.only(top: 12),
                    child: Text(
                      _detail ? '$_total 部本地作品' : '$_total 位人物',
                      style: Theme.of(context).textTheme.titleSmall,
                    ),
                  ),
                ],
              ),
            ),
          ),
          SliverPadding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            sliver: SliverGrid.builder(
              gridDelegate: SliverGridDelegateWithMaxCrossAxisExtent(
                maxCrossAxisExtent: _detail ? 220 : 270,
                mainAxisExtent: _detail ? 290 : 300,
                crossAxisSpacing: 12,
                mainAxisSpacing: 12,
              ),
              itemCount: _detail ? _works.length : _people.length,
              itemBuilder: (context, index) {
                if (_detail) {
                  final item = _works[index];
                  return Card(
                    clipBehavior: Clip.antiAlias,
                    child: InkWell(
                      onTap: () => widget.onOpenItem(item.id),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Expanded(child: _cover(item.coverUrl, item.rating)),
                          Padding(
                            padding: const EdgeInsets.all(12),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  item.displayTitle,
                                  maxLines: 2,
                                  overflow: TextOverflow.ellipsis,
                                ),
                                const SizedBox(height: 4),
                                Text(
                                  item.contentState == 'ready'
                                      ? '可阅读'
                                      : '正文未完整下载',
                                  style: Theme.of(context).textTheme.bodySmall,
                                ),
                              ],
                            ),
                          ),
                        ],
                      ),
                    ),
                  );
                }
                final person = _people[index];
                return Card(
                  clipBehavior: Clip.antiAlias,
                  child: InkWell(
                    onTap: () => _open(person),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        Expanded(
                          child: person.covers.isEmpty
                              ? _cover(null, 'unknown')
                              : Row(
                                  crossAxisAlignment:
                                      CrossAxisAlignment.stretch,
                                  children: [
                                    for (final cover in person.covers)
                                      Expanded(
                                        child: _cover(
                                          cover['cover_url'] as String?,
                                          cover['rating'] as String? ??
                                              'unknown',
                                        ),
                                      ),
                                  ],
                                ),
                        ),
                        Padding(
                          padding: const EdgeInsets.all(14),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                person.displayName,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: Theme.of(context).textTheme.titleMedium,
                              ),
                              const SizedBox(height: 6),
                              Text(
                                '${person.count} 部作品',
                                style: Theme.of(context).textTheme.bodySmall,
                              ),
                              Text(
                                person.profile == null ? '待补全人物资料' : '已保存人物资料',
                                style: Theme.of(context).textTheme.bodySmall,
                              ),
                            ],
                          ),
                        ),
                      ],
                    ),
                  ),
                );
              },
            ),
          ),
          if (_loading)
            const SliverToBoxAdapter(
              child: Padding(
                padding: EdgeInsets.all(24),
                child: Center(child: CircularProgressIndicator()),
              ),
            ),
          if (_error != null)
            SliverToBoxAdapter(
              child: Padding(
                padding: const EdgeInsets.all(20),
                child: Column(
                  children: [
                    Text(_error!),
                    TextButton(
                      onPressed: () => _load(append: _failedMore),
                      child: const Text('重试'),
                    ),
                  ],
                ),
              ),
            ),
          if (!_loading &&
              _error == null &&
              (_detail ? _works.isEmpty : _people.isEmpty))
            SliverToBoxAdapter(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Text(
                  _detail
                      ? '此筛选下暂无本地作品。给作品添加对应角色标签后，会自动汇集到这里。'
                      : '暂无人物。AI 识别或人工添加角色标签后会自动展示，也可以联网添加人物资料。',
                ),
              ),
            ),
          if (_more)
            SliverToBoxAdapter(
              child: TextButton(
                onPressed: _loading ? null : () => _load(append: true),
                child: Text(_detail ? '加载更多作品' : '加载更多人物'),
              ),
            ),
          const SliverToBoxAdapter(child: SizedBox(height: 24)),
        ],
      ),
    ),
  );
}

class CharacterResearchScreen extends StatefulWidget {
  final LibraryApi api;
  final WallCharacter? character;
  const CharacterResearchScreen({super.key, required this.api, this.character});
  @override
  State<CharacterResearchScreen> createState() =>
      _CharacterResearchScreenState();
}

class _CharacterResearchScreenState extends State<CharacterResearchScreen> {
  late final TextEditingController _query;
  String _mode = 'character';
  List<Map<String, dynamic>> _candidates = [];
  bool _loading = false, _saving = false, _searched = false;
  String? _error;
  int _request = 0;
  @override
  void initState() {
    super.initState();
    _query = TextEditingController(
      text: widget.character?.name.replaceAll('_', ' ') ?? '',
    );
  }

  @override
  void dispose() {
    _request++;
    _query.dispose();
    super.dispose();
  }

  Future<void> _search() async {
    if (_loading || _saving || _query.text.trim().isEmpty) return;
    final request = ++_request;
    setState(() {
      _loading = true;
      _error = null;
      _candidates = [];
      _searched = false;
    });
    try {
      final data =
          await widget.api.post('/characters/search', {
                'query': _query.text.trim(),
                'mode': _mode,
                'limit': 6,
              })
              as Map;
      if (!mounted || request != _request) return;
      setState(() {
        _candidates = (data['candidates'] as List)
            .map((v) => Map<String, dynamic>.from(v as Map))
            .toList();
        _searched = true;
      });
    } catch (e) {
      if (mounted && request == _request) setState(() => _error = _message(e));
    } finally {
      if (mounted && request == _request) setState(() => _loading = false);
    }
  }

  Future<void> _confirm(Map<String, dynamic> candidate) async {
    if (_loading || _saving) return;
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      final target = widget.character?.id;
      final result =
          await widget.api.post(
                target == null
                    ? '/characters'
                    : '/characters/${Uri.encodeComponent(target)}/profile',
                {'candidate_id': int.parse('${candidate['id']}')},
              )
              as Map;
      if (mounted) {
        Navigator.pop(
          context,
          WallCharacter.fromJson(Map<String, dynamic>.from(result)),
        );
      }
    } catch (e) {
      if (mounted) setState(() => _error = _message(e));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) => PopScope(
    canPop: !_saving,
    child: Scaffold(
      appBar: AppBar(title: const Text('联网人物资料')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text('搜索词发送到 AniList，不上传书页。核对人物后保存简介、别名及原作信息，之后查看不再查询外网。'),
          const SizedBox(height: 16),
          DropdownButtonFormField<String>(
            initialValue: _mode,
            decoration: const InputDecoration(labelText: '查找方式'),
            items: const [
              DropdownMenuItem(value: 'character', child: Text('人物名')),
              DropdownMenuItem(value: 'work', child: Text('作品名找人物')),
            ],
            onChanged: _saving || _loading
                ? null
                : (value) => setState(() => _mode = value!),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _query,
            maxLength: 120,
            enabled: !_saving && !_loading,
            onSubmitted: (_) => _search(),
            decoration: const InputDecoration(
              labelText: '搜索词',
              prefixIcon: Icon(Icons.search),
            ),
          ),
          FilledButton.icon(
            onPressed: _saving || _loading ? null : _search,
            icon: const Icon(Icons.travel_explore),
            label: const Text('搜索人物'),
          ),
          if (_loading || _saving) const LinearProgressIndicator(),
          if (_error != null)
            Padding(padding: const EdgeInsets.all(12), child: Text(_error!)),
          if (_searched && _candidates.isEmpty)
            const Padding(
              padding: EdgeInsets.all(16),
              child: Text('没有匹配人物，请尝试其他名字或作品名。'),
            ),
          for (final candidate in _candidates)
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      candidate['native_name'] as String? ??
                          candidate['name'] as String,
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                    Text('${candidate['name']}'),
                    Text((candidate['aliases'] as List? ?? []).join(' / ')),
                    Text(
                      (candidate['works'] as List? ?? [])
                          .map((work) => (work as Map)['title'])
                          .join(' · '),
                    ),
                    const SizedBox(height: 10),
                    Text(
                      candidate['description'] as String? ?? '暂无人物介绍',
                      maxLines: 5,
                      overflow: TextOverflow.ellipsis,
                    ),
                    if (_sourceURL(candidate['source_url']).isNotEmpty)
                      SelectableText(
                        _sourceURL(candidate['source_url']),
                        style: Theme.of(context).textTheme.bodySmall,
                      ),
                    const SizedBox(height: 12),
                    FilledButton.tonal(
                      onPressed: _saving || _loading
                          ? null
                          : () => _confirm(candidate),
                      child: const Text('确认此人物并保存'),
                    ),
                  ],
                ),
              ),
            ),
        ],
      ),
    ),
  );
}
