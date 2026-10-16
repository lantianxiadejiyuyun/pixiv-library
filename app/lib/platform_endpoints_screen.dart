import 'package:flutter/material.dart';
import 'api.dart';

class PlatformEndpoint {
  final String platform, label, defaultUrl, effectiveUrl, description;
  final String? customUrl;
  final bool enabled;
  const PlatformEndpoint({
    required this.platform,
    required this.label,
    required this.defaultUrl,
    required this.effectiveUrl,
    required this.description,
    required this.customUrl,
    required this.enabled,
  });
  factory PlatformEndpoint.fromJson(Map<String, dynamic> json) =>
      PlatformEndpoint(
        platform: json['platform'] as String,
        label: json['label'] as String,
        defaultUrl: json['default_url'] as String,
        effectiveUrl: json['effective_url'] as String,
        description: json['description'] as String? ?? '',
        customUrl: json['custom_url'] as String?,
        enabled: json['enabled'] == true,
      );
}

String _message(Object error) => error is ApiFailure
    ? switch (error.status) {
        403 => '管理员密码错误或无权修改，请检查密码后重试',
        400 => '接口地址不可用，请检查公网 HTTPS 地址和网络',
        404 => '服务器尚不支持平台 API 设置，请更新服务器',
        _ => error.message,
      }
    : '无法保存平台设置，请重试';

class PlatformEndpointsScreen extends StatefulWidget {
  final LibraryApi api;
  const PlatformEndpointsScreen({super.key, required this.api});
  @override
  State<PlatformEndpointsScreen> createState() =>
      _PlatformEndpointsScreenState();
}

class _PlatformEndpointsScreenState extends State<PlatformEndpointsScreen> {
  List<PlatformEndpoint> _items = [];
  bool _loading = true;
  String? _error;
  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final data = await widget.api.get('/platform-endpoints');
      final items = (data['items'] as List)
          .map((row) => PlatformEndpoint.fromJson(row as Map<String, dynamic>))
          .toList();
      if (mounted) setState(() => _items = items);
    } catch (error) {
      if (mounted) {
        setState(
          () => _error = error is ApiFailure ? error.message : '无法读取平台设置，请重试',
        );
      }
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _edit(PlatformEndpoint endpoint) async {
    final saved = await Navigator.of(context).push<PlatformEndpoint>(
      MaterialPageRoute(
        builder: (_) => _EndpointEditor(api: widget.api, endpoint: endpoint),
      ),
    );
    if (mounted && saved != null) {
      setState(() {
        _items = _items
            .map((item) => item.platform == saved.platform ? saved : item)
            .toList();
      });
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('内容平台 API'),
      actions: [
        IconButton(
          tooltip: '刷新平台设置',
          onPressed: _loading ? null : _load,
          icon: const Icon(Icons.refresh),
        ),
      ],
    ),
    body: ListView(
      padding: const EdgeInsets.all(16),
      children: [
        const Text('由服务器统一请求平台接口，修改后对后续请求生效。'),
        const SizedBox(height: 16),
        if (_loading) const LinearProgressIndicator(),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 16),
            child: Text(_error!),
          ),
        for (final endpoint in _items)
          Card(
            child: ListTile(
              contentPadding: const EdgeInsets.all(16),
              title: Text(endpoint.label),
              subtitle: Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  '${endpoint.enabled ? '自定义接口' : '默认接口'}\n${endpoint.effectiveUrl}',
                ),
              ),
              trailing: const Icon(Icons.chevron_right),
              onTap: _loading ? null : () => _edit(endpoint),
            ),
          ),
      ],
    ),
  );
}

class _EndpointEditor extends StatefulWidget {
  final LibraryApi api;
  final PlatformEndpoint endpoint;
  const _EndpointEditor({required this.api, required this.endpoint});
  @override
  State<_EndpointEditor> createState() => _EndpointEditorState();
}

class _EndpointEditorState extends State<_EndpointEditor> {
  final _form = GlobalKey<FormState>();
  late final TextEditingController _url;
  final _password = TextEditingController();
  late bool _enabled;
  bool _saving = false;
  String? _error;
  @override
  void initState() {
    super.initState();
    _url = TextEditingController(text: widget.endpoint.customUrl ?? '');
    _enabled = widget.endpoint.enabled;
  }

  @override
  void dispose() {
    _url.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (_saving || !_form.currentState!.validate()) return;
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      final saved = await widget.api.put(
        '/platform-endpoints/${Uri.encodeComponent(widget.endpoint.platform)}',
        {
          'enabled': _enabled,
          'custom_url': _url.text.trim().isEmpty ? null : _url.text.trim(),
          'admin_password': _password.text,
        },
      );
      _password.clear();
      if (mounted) {
        Navigator.of(
          context,
        ).pop(PlatformEndpoint.fromJson(saved as Map<String, dynamic>));
      }
    } catch (error) {
      if (mounted) {
        setState(() {
          _error = _message(error);
          _saving = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) => PopScope(
    canPop: !_saving,
    child: Scaffold(
      appBar: AppBar(title: Text('${widget.endpoint.label} API')),
      body: Form(
        key: _form,
        child: ListView(
          padding: const EdgeInsets.all(20),
          children: [
            Text(widget.endpoint.description),
            const SizedBox(height: 12),
            Text('默认地址：${widget.endpoint.defaultUrl}'),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('使用自定义 API'),
              value: _enabled,
              onChanged: _saving
                  ? null
                  : (value) => setState(() => _enabled = value),
            ),
            TextFormField(
              controller: _url,
              enabled: !_saving,
              keyboardType: TextInputType.url,
              autocorrect: false,
              decoration: const InputDecoration(
                labelText: '自定义 API 地址',
                hintText: 'https://',
              ),
              validator: (value) {
                if ((value ?? '').trim().isEmpty) {
                  return _enabled ? '请输入自定义 API 地址' : null;
                }
                final uri = Uri.tryParse(value!.trim());
                return uri != null &&
                        uri.scheme == 'https' &&
                        uri.host.isNotEmpty &&
                        uri.userInfo.isEmpty &&
                        !uri.hasQuery &&
                        !uri.hasFragment
                    ? null
                    : '请输入不带账号、查询参数和片段的 HTTPS 地址';
              },
            ),
            const SizedBox(height: 16),
            const Text('自定义接口需兼容平台协议，并会收到该平台请求及必要凭据。'),
            const SizedBox(height: 24),
            TextFormField(
              controller: _password,
              enabled: !_saving,
              obscureText: true,
              autocorrect: false,
              enableSuggestions: false,
              decoration: const InputDecoration(
                labelText: '书库管理员密码',
                helperText: '修改服务器设置时验证，不保存在手机',
              ),
              validator: (value) =>
                  value == null || value.isEmpty ? '请输入管理员密码' : null,
            ),
            const SizedBox(height: 24),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 16),
                child: Text(
                  _error!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              ),
            FilledButton.icon(
              onPressed: _saving ? null : _save,
              icon: _saving
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.save_outlined),
              label: const Text('保存设置'),
            ),
            TextButton(
              onPressed: _saving
                  ? null
                  : () => setState(() => _enabled = false),
              child: const Text('使用默认接口'),
            ),
          ],
        ),
      ),
    ),
  );
}
