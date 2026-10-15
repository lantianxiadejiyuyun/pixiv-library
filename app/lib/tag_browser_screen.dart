import 'dart:async';
import 'package:flutter/material.dart';
import 'api.dart';
import 'models.dart';

class TagBrowserScreen extends StatefulWidget {
  final LibraryApi api;
  final String source;
  const TagBrowserScreen({super.key, required this.api, this.source = ''});
  @override
  State<TagBrowserScreen> createState() => _TagBrowserScreenState();
}

class _TagBrowserScreenState extends State<TagBrowserScreen> {
  final _search = TextEditingController();
  Timer? _debounce;
  List<LibraryTag> _tags = [];
  String _query = '';
  String? _error;
  int _request = 0;
  bool _loading = true, _more = false;

  @override
  void initState() {
    super.initState();
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

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('搜索标签')),
    body: Column(
      children: [
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
                          leading: const Icon(Icons.tag),
                          title: Text(tag.displayName),
                          subtitle: Text(
                            '${tag.nameZh != null ? '${tag.name} · ' : ''}${tag.count} 部作品',
                          ),
                          trailing: const Icon(Icons.chevron_right),
                          onTap: _loading
                              ? null
                              : () => Navigator.of(context).pop(tag),
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
