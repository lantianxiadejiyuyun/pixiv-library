import 'dart:async';
import 'package:flutter/material.dart';
import 'api.dart';
import 'models.dart';
import 'library_search.dart';
import 'tag_tracking_screen.dart';

class TagBrowserScreen extends StatefulWidget {
  final LibraryApi api;
  final String source;
  final TagSelection selection;
  const TagBrowserScreen({
    super.key,
    required this.api,
    this.source = '',
    this.selection = const TagSelection([]),
  });
  @override
  State<TagBrowserScreen> createState() => _TagBrowserScreenState();
}

class _TagBrowserScreenState extends State<TagBrowserScreen> {
  final _search = TextEditingController();
  Timer? _debounce;
  List<LibraryTag> _tags = [];
  late List<LibraryTag> _selected;
  late String _mode;
  String _query = '';
  String? _error;
  int _request = 0;
  bool _loading = true, _more = false;

  @override
  void initState() {
    super.initState();
    _selected = [...widget.selection.tags];
    _mode = widget.selection.mode;
    _load();
  }

  @override
  void dispose() {
    _request++;
    _debounce?.cancel();
    _search.dispose();
    super.dispose();
  }

  Future<void> _load({bool append = false}) async {
    if (append && (_loading || !_more)) return;
    final request = ++_request;
    setState(() {
      _loading = true;
      _error = null;
      if (!append) {
        _tags = [];
        _more = false;
      }
    });
    try {
      final data =
          await widget.api.get(
                '/tags',
                query: {
                  'q': _query,
                  'limit': '100',
                  'offset': append ? '${_tags.length}' : '0',
                  if (widget.source.isNotEmpty) 'source': widget.source,
                },
              )
              as List;
      if (!mounted || request != _request) return;
      final tags = data
          .map((v) => LibraryTag.fromJson(v as Map<String, dynamic>))
          .toList();
      setState(() {
        _tags = append ? [..._tags, ...tags] : tags;
        _more = tags.length == 100;
        _loading = false;
      });
    } catch (e) {
      if (mounted && request == _request) {
        setState(() {
          _error = e is ApiFailure ? e.message : '无法读取标签';
          _loading = false;
        });
      }
    }
  }

  void _changed(String value) {
    _debounce?.cancel();
    // Invalidate an old in-flight response immediately, before debounce elapses.
    _request++;
    _debounce = Timer(const Duration(milliseconds: 350), () {
      _query = value.trim();
      _load();
    });
  }

  void _toggle(LibraryTag tag) {
    setState(() {
      if (_selected.any((t) => t.id == tag.id)) {
        _selected.removeWhere((t) => t.id == tag.id);
      } else if (_selected.length < 20) {
        _selected.add(tag);
      }
    });
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('搜索标签'),
      actions: [
        IconButton(
          tooltip: '追踪标签与下载',
          icon: const Icon(Icons.radar),
          onPressed: () => Navigator.of(context).push(
            MaterialPageRoute<void>(
              builder: (_) => TagTrackingScreen(
                api: widget.api,
                tags: _selected.map((v) => v.name).toList(),
                platform: widget.source,
              ),
            ),
          ),
        ),
        TextButton(
          onPressed: () =>
              Navigator.of(context).pop(TagSelection([..._selected], _mode)),
          child: const Text('查看作品'),
        ),
      ],
    ),
    body: Column(
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Row(
            children: [
              Expanded(child: Text('已选 ${_selected.length} / 20')),
              DropdownButton<String>(
                value: _mode,
                onChanged: (v) => setState(() => _mode = v!),
                items: const [
                  DropdownMenuItem(value: 'all', child: Text('全部匹配')),
                  DropdownMenuItem(value: 'any', child: Text('匹配任一')),
                ],
              ),
              TextButton(
                onPressed: _selected.isEmpty
                    ? null
                    : () => setState(() => _selected = []),
                child: const Text('清空'),
              ),
            ],
          ),
        ),
        if (_selected.isNotEmpty)
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: Row(
              children: [
                for (final tag in _selected)
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: InputChip(
                      label: Text(tag.displayName),
                      onDeleted: () => _toggle(tag),
                    ),
                  ),
              ],
            ),
          ),
        Padding(
          padding: const EdgeInsets.all(16),
          child: TextField(
            controller: _search,
            autofocus: true,
            onChanged: _changed,
            textInputAction: TextInputAction.search,
            onSubmitted: (value) {
              _debounce?.cancel();
              _query = value.trim();
              _load();
            },
            decoration: const InputDecoration(
              hintText: '搜索原始标签或中文译名',
              prefixIcon: Icon(Icons.tag),
            ),
          ),
        ),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              children: [
                Text(_error!),
                TextButton(onPressed: () => _load(), child: const Text('重试')),
              ],
            ),
          ),
        if (_loading) const LinearProgressIndicator(),
        Expanded(
          child: !_loading && _tags.isEmpty && _error == null
              ? const Center(child: Text('没有匹配的标签'))
              : NotificationListener<ScrollNotification>(
                  onNotification: (event) {
                    if (event is ScrollUpdateNotification &&
                        event.metrics.extentAfter < 300 &&
                        !_loading &&
                        _more &&
                        _error == null) {
                      _load(append: true);
                    }
                    return false;
                  },
                  child: RefreshIndicator(
                    onRefresh: () => _load(),
                    child: ListView.builder(
                      physics: const AlwaysScrollableScrollPhysics(),
                      itemCount: _tags.length + (_more ? 1 : 0),
                      itemBuilder: (context, index) {
                        if (index == _tags.length) {
                          return TextButton(
                            onPressed: _loading
                                ? null
                                : () => _load(append: true),
                            child: const Text('加载更多标签'),
                          );
                        }
                        final tag = _tags[index];
                        return ListTile(
                          leading: Icon(
                            _selected.any((t) => t.id == tag.id)
                                ? Icons.check_box
                                : Icons.check_box_outline_blank,
                          ),
                          selected: _selected.any((t) => t.id == tag.id),
                          title: Text(tag.displayName),
                          subtitle: Text(
                            '${tag.nameZh != null ? '${tag.name} · ' : ''}${tag.count} 部作品',
                          ),
                          onTap:
                              _loading ||
                                  (_selected.length >= 20 &&
                                      !_selected.any((t) => t.id == tag.id))
                              ? null
                              : () => _toggle(tag),
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
