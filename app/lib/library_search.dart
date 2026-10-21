import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'api.dart';
import 'models.dart';

class TagSelection {
  final List<LibraryTag> tags;
  final String mode;
  const TagSelection(this.tags, [this.mode = 'all']);
}

class SearchEntry extends TagSelection {
  final String query;
  const SearchEntry(this.query, super.tags, [super.mode]);
  String get identity =>
      jsonEncode([query.trim(), tags.map((t) => t.id).toList()..sort(), mode]);
  String get label => [
    if (query.isNotEmpty) query,
    ...tags.map((t) => '#${t.displayName}'),
  ].join(' · ');
  Map<String, dynamic> toJson() => {
    'query': query,
    'mode': mode,
    'tags': tags
        .map((t) => {'id': t.id, 'name': t.name, 'name_zh': t.nameZh})
        .toList(),
  };
  static SearchEntry? parse(dynamic value) {
    if (value is! Map<String, dynamic> ||
        value['query'] is! String ||
        (value['query'] as String).length > 512 ||
        !['all', 'any'].contains(value['mode']) ||
        value['tags'] is! List) {
      return null;
    }
    final tags = value['tags'] as List;
    if (tags.length > 20 ||
        tags.any(
          (t) =>
              t is! Map<String, dynamic> ||
              t['id'] is! String ||
              (t['id'] as String).length > 36 ||
              t['name'] is! String ||
              (t['name_zh'] != null && t['name_zh'] is! String),
        )) {
      return null;
    }
    if ((value['query'] as String).trim().isEmpty && tags.isEmpty) return null;
    return SearchEntry(
      (value['query'] as String).trim(),
      tags.map((t) => LibraryTag.fromJson(t as Map<String, dynamic>)).toList(),
      value['mode'] as String,
    );
  }
}

class LibrarySearchController extends ChangeNotifier {
  final LibraryApi api;
  final FlutterSecureStorage storage;
  LibrarySearchController(
    this.api, {
    this.storage = const FlutterSecureStorage(),
  }) {
    ready = _read();
  }
  late final Future<void> ready;
  String get _key => 'qiye.search.v1.${Uri.encodeComponent(api.server.origin)}';
  List<SearchEntry> history = [];
  List<LibraryTag> suggestions = [];
  String query = '', source = '';
  Set<String> selected = {};
  bool visible = false, loading = false, _disposed = false;
  String? error;
  int _revision = 0, _historyRevision = 0;
  Timer? _timer;
  Future<void> _writes = Future.value();

  Future<void> _read() async {
    final revision = _historyRevision;
    try {
      final data = jsonDecode(await storage.read(key: _key) ?? '[]');
      if (!_disposed && revision == _historyRevision && data is List) {
        history = data
            .map(SearchEntry.parse)
            .whereType<SearchEntry>()
            .take(20)
            .toList();
        notifyListeners();
      }
    } catch (_) {
      /* A locked/unavailable store must not block searching. */
    }
  }

  void _save() {
    _historyRevision++;
    final value = jsonEncode(history.map((e) => e.toJson()).toList());
    _writes = _writes.then((_) async {
      try {
        await storage.write(key: _key, value: value);
      } catch (_) {}
    });
  }

  Future<void> get saved => _writes;
  void remember(SearchEntry entry) {
    if (entry.query.trim().isEmpty && entry.tags.isEmpty) return;
    history = [
      entry,
      ...history.where((old) => old.identity != entry.identity),
    ].take(20).toList();
    _save();
    notifyListeners();
  }

  void remove([SearchEntry? entry]) {
    history = entry == null
        ? []
        : history.where((old) => old.identity != entry.identity).toList();
    _save();
    notifyListeners();
  }

  void show(bool value) {
    visible = value;
    update(query, source, selected);
  }

  void update(String value, String platform, Set<String> tags) {
    query = value.trim();
    source = platform;
    selected = tags;
    _revision++;
    _timer?.cancel();
    suggestions = [];
    error = null;
    loading = visible && query.isNotEmpty;
    notifyListeners();
    if (loading) _timer = Timer(const Duration(milliseconds: 250), suggest);
  }

  Future<void> suggest() async {
    if (!visible || query.isEmpty) return;
    final revision = ++_revision;
    loading = true;
    error = null;
    notifyListeners();
    try {
      final data =
          await api.get(
                '/tags',
                query: {
                  'q': query.length > 191 ? query.substring(0, 191) : query,
                  'limit': '12',
                  'suggest': 'true',
                  if (source.isNotEmpty) 'source': source,
                },
              )
              as List;
      if (!_disposed && revision == _revision) {
        suggestions = data
            .map((v) => LibraryTag.fromJson(v as Map<String, dynamic>))
            .where((t) => !selected.contains(t.id))
            .take(8)
            .toList();
      }
    } catch (e) {
      if (!_disposed && revision == _revision) {
        error = e is ApiFailure ? e.message : '标签联想暂不可用，可继续搜索关键词';
      }
    } finally {
      if (!_disposed && revision == _revision) {
        loading = false;
        notifyListeners();
      }
    }
  }

  @override
  void dispose() {
    _disposed = true;
    _revision++;
    _timer?.cancel();
    super.dispose();
  }
}

class LibrarySearchPanel extends StatelessWidget {
  final LibrarySearchController controller;
  final ValueChanged<LibraryTag> onTag;
  final ValueChanged<SearchEntry> onHistory;
  final VoidCallback onBrowse, onClose;
  const LibrarySearchPanel({
    super.key,
    required this.controller,
    required this.onTag,
    required this.onHistory,
    required this.onBrowse,
    required this.onClose,
  });
  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: controller,
    builder: (context, _) {
      if (!controller.visible) return const SizedBox.shrink();
      final hasQuery = controller.query.isNotEmpty;
      final maxHeight =
          (MediaQuery.sizeOf(context).height -
                  MediaQuery.viewInsetsOf(context).bottom -
                  270)
              .clamp(100.0, 240.0);
      return ConstrainedBox(
        constraints: BoxConstraints(maxHeight: maxHeight),
        child: Material(
          color: Theme.of(context).colorScheme.surfaceContainerLow,
          child: ListView(
            shrinkWrap: true,
            padding: const EdgeInsets.symmetric(horizontal: 16),
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      hasQuery ? '标签联想' : '搜索历史',
                      style: Theme.of(context).textTheme.titleSmall,
                    ),
                  ),
                  if (!hasQuery && controller.history.isNotEmpty)
                    TextButton(
                      onPressed: controller.remove,
                      child: const Text('清空历史'),
                    ),
                  IconButton(
                    onPressed: onClose,
                    tooltip: '收起搜索建议',
                    icon: const Icon(Icons.close),
                  ),
                ],
              ),
              if (hasQuery) ...[
                if (controller.loading) const LinearProgressIndicator(),
                if (controller.error != null)
                  ListTile(
                    title: Text(controller.error!),
                    trailing: TextButton(
                      onPressed: controller.suggest,
                      child: const Text('重试'),
                    ),
                  ),
                if (!controller.loading &&
                    controller.error == null &&
                    controller.suggestions.isEmpty)
                  const ListTile(title: Text('没有匹配标签，可直接搜索关键词')),
                for (final tag in controller.suggestions)
                  ListTile(
                    dense: true,
                    leading: const Icon(Icons.tag),
                    title: Text(tag.displayName),
                    subtitle: tag.nameZh == null ? null : Text(tag.name),
                    trailing: Text('${tag.count} 部'),
                    onTap: () => onTag(tag),
                  ),
              ] else ...[
                if (controller.history.isEmpty)
                  const ListTile(title: Text('暂无搜索历史')),
                for (final entry in controller.history)
                  ListTile(
                    dense: true,
                    leading: const Icon(Icons.history),
                    title: Text(
                      entry.label,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                    ),
                    subtitle: entry.tags.length > 1
                        ? Text(entry.mode == 'all' ? '全部匹配' : '匹配任一')
                        : null,
                    onTap: () => onHistory(entry),
                    trailing: IconButton(
                      tooltip: '删除搜索记录 ${entry.label}',
                      onPressed: () => controller.remove(entry),
                      icon: const Icon(Icons.close, size: 18),
                    ),
                  ),
              ],
              TextButton(onPressed: onBrowse, child: const Text('选择多个标签')),
            ],
          ),
        ),
      );
    },
  );
}
