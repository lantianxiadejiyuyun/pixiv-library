import 'package:flutter/material.dart';
import 'api.dart';
import 'models.dart';

String _error(Object error) => error is ApiFailure ? error.message : '操作失败，请重试';

Future<Map<String, dynamic>?> _groupName(
  BuildContext context,
  LibraryApi api, [
  Map<String, dynamic>? group,
]) => showDialog<Map<String, dynamic>>(
  context: context,
  builder: (_) => _GroupDialog(api: api, group: group),
);

class _GroupDialog extends StatefulWidget {
  final LibraryApi api;
  final Map<String, dynamic>? group;
  const _GroupDialog({required this.api, this.group});
  @override
  State<_GroupDialog> createState() => _GroupDialogState();
}

class _GroupDialogState extends State<_GroupDialog> {
  late final TextEditingController _input;
  bool _busy = false;
  String? _message;
  @override
  void initState() {
    super.initState();
    _input = TextEditingController(
      text: widget.group?['name'] as String? ?? '',
    );
  }

  @override
  void dispose() {
    _input.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (_input.text.trim().isEmpty) {
      setState(() => _message = '请输入分组名称');
      return;
    }
    setState(() {
      _busy = true;
      _message = null;
    });
    try {
      final value = widget.group == null
          ? await widget.api.post('/favorite-groups', {
              'name': _input.text.trim(),
            })
          : await widget.api.put('/favorite-groups/${widget.group!['id']}', {
              'name': _input.text.trim(),
            });
      if (mounted) {
        Navigator.pop(context, Map<String, dynamic>.from(value as Map));
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _busy = false;
          _message = _error(e);
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
    title: Text(widget.group == null ? '新建收藏分组' : '重命名分组'),
    content: TextField(
      controller: _input,
      autofocus: true,
      maxLength: 80,
      enabled: !_busy,
      decoration: InputDecoration(labelText: '分组名称', errorText: _message),
    ),
    actions: [
      TextButton(
        onPressed: _busy ? null : () => Navigator.pop(context),
        child: const Text('取消'),
      ),
      FilledButton(
        onPressed: _busy ? null : _save,
        child: Text(_busy ? '保存中…' : '保存'),
      ),
    ],
  );
}

Future<Map<String, dynamic>?> editFavorite(
  BuildContext context,
  LibraryApi api,
  LibraryItem item,
) => showModalBottomSheet<Map<String, dynamic>>(
  context: context,
  isScrollControlled: true,
  showDragHandle: true,
  builder: (_) => FractionallySizedBox(
    heightFactor: .85,
    child: FavoriteEditor(api: api, item: item),
  ),
);

class FavoriteEditor extends StatefulWidget {
  final LibraryApi api;
  final LibraryItem item;
  const FavoriteEditor({super.key, required this.api, required this.item});
  @override
  State<FavoriteEditor> createState() => _FavoriteEditorState();
}

class _FavoriteEditorState extends State<FavoriteEditor> {
  List<Map<String, dynamic>> _groups = [];
  late final Set<String> _selected;
  late int _score;
  bool _loading = true, _busy = false;
  String? _message;
  @override
  void initState() {
    super.initState();
    _selected = widget.item.favoriteGroupIds.toSet();
    _score = widget.item.userScore;
    _load();
  }

  Future<void> _load() async {
    try {
      final value = await widget.api.get('/favorite-groups') as Map;
      if (!mounted) return;
      setState(() {
        _groups = (value['items'] as List)
            .map((v) => Map<String, dynamic>.from(v as Map))
            .toList();
        _selected.removeWhere((id) => !_groups.any((g) => g['id'] == id));
        _message = null;
        _loading = false;
      });
    } catch (e) {
      if (mounted) {
        setState(() {
          _message = _error(e);
          _loading = false;
        });
      }
    }
  }

  Future<void> _save(bool favorite) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _message = null;
    });
    try {
      final value = await widget.api.put('/items/${widget.item.id}/favorite', {
        'favorite': favorite,
        if (favorite) 'group_ids': _selected.toList(),
        'score': _score,
      });
      if (mounted) {
        Navigator.pop(context, Map<String, dynamic>.from(value as Map));
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _message = _error(e);
          _busy = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) => SafeArea(
    top: false,
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          child: Text('收藏与评分', style: Theme.of(context).textTheme.titleLarge),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(24, 8, 24, 0),
          child: Text(
            widget.item.displayTitle,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
          ),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
          child: Row(
            children: [
              for (int i = 1; i <= 5; i++)
                IconButton(
                  tooltip: '$i 星',
                  onPressed: _busy ? null : () => setState(() => _score = i),
                  icon: Icon(
                    i <= _score
                        ? Icons.star_rounded
                        : Icons.star_outline_rounded,
                    color: Theme.of(context).colorScheme.primary,
                  ),
                ),
              TextButton(
                onPressed: _busy ? null : () => setState(() => _score = 0),
                child: const Text('清除评分'),
              ),
            ],
          ),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          child: Text(
            _selected.isEmpty ? '未分组 · 可多选分组' : '已选 ${_selected.length} 个分组',
          ),
        ),
        Expanded(
          child: _loading
              ? const Center(child: CircularProgressIndicator())
              : ListView(
                  children: [
                    for (final group in _groups)
                      CheckboxListTile(
                        title: Text(group['name'] as String),
                        value: _selected.contains(group['id']),
                        onChanged: _busy
                            ? null
                            : (selected) => setState(() {
                                if (selected == true) {
                                  _selected.add(group['id'] as String);
                                } else {
                                  _selected.remove(group['id']);
                                }
                              }),
                      ),
                    ListTile(
                      leading: const Icon(Icons.create_new_folder_outlined),
                      title: const Text('新建分组'),
                      enabled: !_busy,
                      onTap: () async {
                        final group = await _groupName(context, widget.api);
                        if (!mounted || group == null) return;
                        _selected.add(group['id'] as String);
                        await _load();
                      },
                    ),
                  ],
                ),
        ),
        if (_message != null)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    _message!,
                    style: TextStyle(
                      color: Theme.of(context).colorScheme.error,
                    ),
                  ),
                ),
                TextButton(
                  onPressed: _busy ? null : _load,
                  child: const Text('重试读取'),
                ),
              ],
            ),
          ),
        Padding(
          padding: const EdgeInsets.all(16),
          child: Row(
            children: [
              if (widget.item.isFavorite)
                TextButton(
                  onPressed: _busy ? null : () => _save(false),
                  child: const Text('取消收藏'),
                ),
              const Spacer(),
              FilledButton.icon(
                onPressed:
                    _busy || _loading || (_groups.isEmpty && _message != null)
                    ? null
                    : () => _save(true),
                icon: const Icon(Icons.favorite),
                label: Text(_busy ? '保存中…' : '保存收藏'),
              ),
            ],
          ),
        ),
      ],
    ),
  );
}

class FavoriteGroupsScreen extends StatefulWidget {
  final LibraryApi api;
  final Future<void> Function(String id, String name) onOpen;
  const FavoriteGroupsScreen({
    super.key,
    required this.api,
    required this.onOpen,
  });
  @override
  State<FavoriteGroupsScreen> createState() => _FavoriteGroupsScreenState();
}

class _FavoriteGroupsScreenState extends State<FavoriteGroupsScreen> {
  Map<String, dynamic>? _data;
  String? _message;
  bool _busy = false;
  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final value = await widget.api.get('/favorite-groups') as Map;
      if (mounted) {
        setState(() {
          _data = Map<String, dynamic>.from(value);
          _message = null;
        });
      }
    } catch (e) {
      if (mounted) setState(() => _message = _error(e));
    }
  }

  Future<void> _open(String id, String name) async {
    await widget.onOpen(id, name);
    if (mounted) _load();
  }

  Future<void> _remove(Map<String, dynamic> group) async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('删除「${group['name']}」？'),
        content: const Text('只删除分组，作品仍保留在我的收藏中。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('删除分组'),
          ),
        ],
      ),
    );
    if (!mounted || yes != true) return;
    setState(() => _busy = true);
    try {
      await widget.api.delete('/favorite-groups/${group['id']}');
      await _load();
    } catch (e) {
      if (mounted) setState(() => _message = _error(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('我的收藏'),
      actions: [
        IconButton(
          tooltip: '新建收藏分组',
          icon: const Icon(Icons.create_new_folder_outlined),
          onPressed: _busy
              ? null
              : () async {
                  if (await _groupName(context, widget.api) != null &&
                      mounted) {
                    _load();
                  }
                },
        ),
      ],
    ),
    body: _data == null && _message == null
        ? const Center(child: CircularProgressIndicator())
        : RefreshIndicator(
            onRefresh: _load,
            child: ListView(
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.all(12),
              children: [
                if (_message != null)
                  ListTile(
                    title: Text(_message!),
                    trailing: TextButton(
                      onPressed: _load,
                      child: const Text('重试'),
                    ),
                  ),
                if (_data != null) ...[
                  ListTile(
                    leading: const Icon(Icons.favorite_outline),
                    title: const Text('全部收藏'),
                    trailing: Text('${_data!['total_favorites']}'),
                    onTap: () => _open('', '全部收藏'),
                  ),
                  ListTile(
                    leading: const Icon(Icons.inbox_outlined),
                    title: const Text('未分组'),
                    trailing: Text('${_data!['ungrouped_count']}'),
                    onTap: () => _open('ungrouped', '未分组'),
                  ),
                  const Divider(),
                  for (final raw in _data!['items'] as List)
                    Builder(
                      builder: (context) {
                        final group = Map<String, dynamic>.from(raw as Map);
                        return ListTile(
                          leading: const Icon(Icons.folder_outlined),
                          title: Text(group['name'] as String),
                          subtitle: Text('${group['count']} 部作品'),
                          onTap: _busy
                              ? null
                              : () => _open(
                                  group['id'] as String,
                                  group['name'] as String,
                                ),
                          trailing: PopupMenuButton<String>(
                            enabled: !_busy,
                            tooltip: '管理分组 ${group['name']}',
                            onSelected: (value) async {
                              if (value == 'delete') {
                                await _remove(group);
                              } else {
                                if (await _groupName(
                                          context,
                                          widget.api,
                                          group,
                                        ) !=
                                        null &&
                                    mounted) {
                                  _load();
                                }
                              }
                            },
                            itemBuilder: (_) => [
                              const PopupMenuItem(
                                value: 'rename',
                                child: Text('重命名'),
                              ),
                              const PopupMenuItem(
                                value: 'delete',
                                child: Text('删除分组'),
                              ),
                            ],
                          ),
                        );
                      },
                    ),
                ],
              ],
            ),
          ),
  );
}
