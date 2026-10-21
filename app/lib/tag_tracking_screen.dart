import 'package:flutter/material.dart';
import 'api.dart';

class TagTrackingScreen extends StatefulWidget {
  final LibraryApi api;
  final List<String> tags;
  final String platform;
  const TagTrackingScreen({
    super.key,
    required this.api,
    this.tags = const [],
    this.platform = 'pixiv',
  });
  @override
  State<TagTrackingScreen> createState() => _TagTrackingScreenState();
}

class _TagTrackingScreenState extends State<TagTrackingScreen> {
  late final TextEditingController _tags;
  late String _platform;
  String _scope = 'all';
  int _interval = 360;
  bool _download = true, _busy = false;
  String? _error, _status;
  List<Map<String, dynamic>> _rows = [];
  @override
  void initState() {
    super.initState();
    _tags = TextEditingController(text: widget.tags.join('\n'));
    _platform = ['pixiv', 'picacg', 'kemono'].contains(widget.platform)
        ? widget.platform
        : 'pixiv';
    _action(_load);
  }

  @override
  void dispose() {
    _tags.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    final data = await widget.api.get('/tag-tracking') as Map;
    if (mounted) {
      setState(
        () => _rows = (data['items'] as List)
            .map((v) => Map<String, dynamic>.from(v as Map))
            .toList(),
      );
    }
  }

  Future<void> _action(Future<void> Function() work, [String? message]) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
      _status = null;
    });
    try {
      await work();
      if (message != null) await _load();
      if (mounted) setState(() => _status = message);
    } catch (e) {
      if (mounted) {
        setState(() => _error = e is ApiFailure ? e.message : '标签追踪请求失败，请重试');
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _create() => _action(() async {
    final tags = _tags.text
        .split(RegExp(r'[\n,，]+'))
        .map((v) => v.trim())
        .where((v) => v.isNotEmpty)
        .toSet()
        .toList();
    if (tags.isEmpty || tags.length > 20) {
      throw const ApiFailure('请填写 1～20 个标签，每行一个');
    }
    await widget.api.post('/tag-tracking', {
      'platform': _platform,
      'tags': tags,
      'auto_download': _download,
      'content_scope': _scope,
      'interval_minutes': _interval,
    });
    _tags.clear();
  }, '追踪已保存，检查任务已排队');
  Future<void> _patch(Map row, Map<String, dynamic> changes) =>
      _action(() async {
        await widget.api.manage(
          'PATCH',
          '/tag-tracking/${row['id']}',
          body: changes,
        );
      }, '已保存');

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('标签追踪与下载')),
    body: RefreshIndicator(
      onRefresh: () => _action(_load),
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (_busy) const LinearProgressIndicator(),
          DropdownButtonFormField<String>(
            initialValue: _platform,
            decoration: const InputDecoration(labelText: '平台'),
            items: const [
              DropdownMenuItem(value: 'pixiv', child: Text('Pixiv')),
              DropdownMenuItem(value: 'picacg', child: Text('哔咔 PicACG')),
              DropdownMenuItem(value: 'kemono', child: Text('Kemono')),
            ],
            onChanged: _busy ? null : (v) => setState(() => _platform = v!),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _tags,
            minLines: 2,
            maxLines: 4,
            maxLength: 4000,
            enabled: !_busy,
            decoration: const InputDecoration(
              labelText: '标签',
              hintText: '每行一个原文标签，最多 20 个',
              border: OutlineInputBorder(),
            ),
          ),
          if (_platform == 'pixiv')
            DropdownButtonFormField<String>(
              initialValue: _scope,
              decoration: const InputDecoration(labelText: '追踪内容'),
              items: const [
                DropdownMenuItem(value: 'all', child: Text('插画、漫画与小说')),
                DropdownMenuItem(value: 'images', child: Text('插画 / 漫画')),
                DropdownMenuItem(value: 'novels', child: Text('小说')),
              ],
              onChanged: _busy ? null : (v) => setState(() => _scope = v!),
            ),
          DropdownButtonFormField<int>(
            initialValue: _interval,
            decoration: const InputDecoration(labelText: '检查间隔'),
            items: const [
              DropdownMenuItem(value: 30, child: Text('30 分钟')),
              DropdownMenuItem(value: 60, child: Text('1 小时')),
              DropdownMenuItem(value: 360, child: Text('6 小时')),
              DropdownMenuItem(value: 1440, child: Text('每天')),
            ],
            onChanged: _busy ? null : (v) => setState(() => _interval = v!),
          ),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('自动下载匹配作品'),
            value: _download,
            onChanged: _busy ? null : (v) => setState(() => _download = v),
          ),
          FilledButton.icon(
            onPressed: _busy ? null : _create,
            icon: const Icon(Icons.radar),
            label: const Text('添加标签追踪'),
          ),
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 16),
            child: Text('每个标签独立追踪，每次检查最新一页。检索使用主账号，下载遵守账号权限和队列并发。'),
          ),
          if (_error != null)
            Text(
              _error!,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          if (_status != null) Text(_status!),
          for (final row in _rows)
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '# ${row['target']}',
                      style: Theme.of(context).textTheme.titleMedium,
                    ),
                    Text(
                      '${row['platform']} · ${row['enabled'] == true ? '追踪中' : '已暂停'} · ${row['interval_minutes']} 分钟',
                    ),
                    if (row['last_error'] != null)
                      Text(
                        '${row['last_error']}',
                        style: TextStyle(
                          color: Theme.of(context).colorScheme.error,
                        ),
                      ),
                    SwitchListTile(
                      contentPadding: EdgeInsets.zero,
                      title: const Text('自动下载'),
                      value: row['auto_download'] == true,
                      onChanged: _busy
                          ? null
                          : (v) => _patch(row, {'auto_download': v}),
                    ),
                    Wrap(
                      spacing: 8,
                      children: [
                        TextButton(
                          onPressed: _busy || row['enabled'] != true
                              ? null
                              : () => _action(() async {
                                  await widget.api.post(
                                    '/tag-tracking/${row['id']}/check',
                                  );
                                }, '检查任务已排队'),
                          child: const Text('立即检查'),
                        ),
                        TextButton(
                          onPressed: _busy
                              ? null
                              : () => _patch(row, {
                                  'enabled': row['enabled'] != true,
                                }),
                          child: Text(row['enabled'] == true ? '暂停追踪' : '恢复追踪'),
                        ),
                        TextButton(
                          onPressed: _busy
                              ? null
                              : () => _action(() async {
                                  await widget.api.delete(
                                    '/tag-tracking/${row['id']}',
                                  );
                                }, '已取消追踪，已下载作品保留'),
                          child: const Text('取消追踪'),
                        ),
                      ],
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
