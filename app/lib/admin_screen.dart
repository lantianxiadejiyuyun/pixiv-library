import 'dart:async';
import 'dart:convert';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';
import 'api.dart';
import 'session.dart';
import 'source_labels.dart';

typedef JsonMap = Map<String, dynamic>;

/// This route owns an independent navigator and an in-memory admin cookie.
/// Expiry/logout never removes the user's QR reading session.
class AdminScreen extends StatefulWidget {
  final Uri? server;
  final Future<LibraryApi> Function(
    Uri server,
    String password,
    VoidCallback expired,
  )?
  login;
  const AdminScreen({super.key, this.server, this.login});
  @override
  State<AdminScreen> createState() => _AdminScreenState();
}

class _AdminScreenState extends State<AdminScreen> {
  final _navigator = GlobalKey<NavigatorState>();
  final _changes = ValueNotifier<int>(0);
  LibraryApi? _api;
  bool _expired = false;
  @override
  void dispose() {
    _api?.close();
    _changes.dispose();
    super.dispose();
  }

  void _reset({bool expired = false}) {
    if (!mounted) return;
    _navigator.currentState?.popUntil((route) => route.isFirst);
    final previous = _api;
    setState(() {
      _api = null;
      _expired = expired;
    });
    _changes.value++;
    previous?.close();
  }

  Future<void> _login(Uri server, String password) async {
    final api =
        await (widget.login?.call(
              server,
              password,
              () => _reset(expired: true),
            ) ??
            LibraryApi.administrator(
              server: server,
              password: password,
              onUnauthorized: () => _reset(expired: true),
            ));
    if (!mounted) {
      api.close();
      return;
    }
    setState(() {
      _api = api;
      _expired = false;
    });
    _changes.value++;
  }

  @override
  Widget build(BuildContext context) => PopScope(
    canPop: false,
    onPopInvokedWithResult: (didPop, _) async {
      if (didPop) return;
      if (await _navigator.currentState!.maybePop()) return;
      if (context.mounted) Navigator.of(context).pop();
    },
    child: Navigator(
      key: _navigator,
      onGenerateRoute: (_) => MaterialPageRoute<void>(
        builder: (_) => ValueListenableBuilder<int>(
          valueListenable: _changes,
          builder: (_, _, _) => _api == null
              ? _AdminLogin(
                  server: widget.server,
                  expired: _expired,
                  login: _login,
                )
              : AdminHome(
                  api: _api!,
                  onLogout: () async {
                    try {
                      await _api?.post('/auth/logout');
                    } catch (_) {
                      /* Drop local authority regardless. */
                    }
                    _reset();
                  },
                ),
        ),
      ),
    ),
  );
}

class _AdminLogin extends StatefulWidget {
  final Uri? server;
  final bool expired;
  final Future<void> Function(Uri, String) login;
  const _AdminLogin({this.server, required this.expired, required this.login});
  @override
  State<_AdminLogin> createState() => _AdminLoginState();
}

class _AdminLoginState extends State<_AdminLogin> {
  final _form = GlobalKey<FormState>();
  late final TextEditingController _server;
  final _password = TextEditingController();
  bool _busy = false;
  String? _error;
  @override
  void initState() {
    super.initState();
    _server = TextEditingController(text: widget.server?.toString() ?? '');
  }

  @override
  void dispose() {
    _server.dispose();
    _password.clear();
    _password.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_busy || !_form.currentState!.validate()) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await widget.login(validateServer(_server.text), _password.text);
    } catch (error) {
      if (mounted) {
        setState(
          () => _error = error is ApiFailure ? error.message : '登录失败，请重试',
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('后台管理'),
      leading: BackButton(
        onPressed: () => Navigator.of(context, rootNavigator: true).pop(),
      ),
    ),
    body: Form(
      key: _form,
      child: ListView(
        padding: const EdgeInsets.all(24),
        children: [
          const Icon(Icons.admin_panel_settings_outlined, size: 52),
          const SizedBox(height: 24),
          const Text('使用书库管理员密码登录，可管理当前服务器的全部后台功能。关闭管理页面后需重新登录。'),
          const SizedBox(height: 24),
          if (widget.expired)
            const Padding(
              padding: EdgeInsets.only(bottom: 16),
              child: Text('管理员会话已过期，请重新登录。阅读绑定仍然保留。'),
            ),
          TextFormField(
            controller: _server,
            enabled: !_busy,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: const InputDecoration(labelText: '服务器地址'),
            validator: (v) {
              try {
                validateServer(v ?? '');
                return null;
              } catch (_) {
                return '请输入有效的服务器根地址';
              }
            },
          ),
          const SizedBox(height: 16),
          TextFormField(
            controller: _password,
            enabled: !_busy,
            obscureText: true,
            autocorrect: false,
            enableSuggestions: false,
            onFieldSubmitted: (_) => _submit(),
            decoration: const InputDecoration(
              labelText: '管理员密码',
              helperText: '密码和管理员会话不保存在手机',
            ),
            validator: (v) => v == null || v.isEmpty ? '请输入管理员密码' : null,
          ),
          const SizedBox(height: 24),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.only(bottom: 16),
              child: Text(_error!),
            ),
          FilledButton.icon(
            onPressed: _busy ? null : _submit,
            icon: _busy
                ? const SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.login),
            label: const Text('登录管理后台'),
          ),
        ],
      ),
    ),
  );
}

class AdminCatalog {
  final Map<String, String> groups, labels;
  final List<JsonMap> actions;
  AdminCatalog.fromJson(JsonMap json)
    : groups = Map<String, String>.from(json['groups'] as Map),
      labels = Map<String, String>.from(json['labels'] as Map),
      actions = (json['actions'] as List).cast<JsonMap>();
  String label(String key) => labels[key] ?? key.replaceAll('_', ' ');
}

class AdminHome extends StatefulWidget {
  final LibraryApi api;
  final VoidCallback onLogout;
  const AdminHome({super.key, required this.api, required this.onLogout});
  @override
  State<AdminHome> createState() => _AdminHomeState();
}

class _AdminHomeState extends State<AdminHome> {
  AdminCatalog? _catalog;
  Object? _error;
  bool _loading = true;
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
      final data = await widget.api.get('/mobile-admin/catalog');
      if (mounted) {
        setState(() => _catalog = AdminCatalog.fromJson(data as JsonMap));
      }
    } catch (error) {
      if (mounted) setState(() => _error = error);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  static const _icons = {
    'overview': Icons.dashboard_outlined,
    'sources': Icons.manage_accounts_outlined,
    'queue': Icons.cloud_download_outlined,
    'subscriptions': Icons.subscriptions_outlined,
    'network': Icons.route_outlined,
    'ai': Icons.auto_awesome_outlined,
    'library': Icons.library_books_outlined,
    'devices': Icons.settings_outlined,
  };
  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('管理工作台'),
      leading: BackButton(
        onPressed: () => Navigator.of(context, rootNavigator: true).pop(),
      ),
      actions: [
        IconButton(
          onPressed: _loading ? null : _load,
          tooltip: '刷新管理功能',
          icon: const Icon(Icons.refresh),
        ),
        IconButton(
          onPressed: widget.onLogout,
          tooltip: '退出管理员登录',
          icon: const Icon(Icons.logout),
        ),
      ],
    ),
    body: ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Text(
          widget.api.server.toString(),
          style: Theme.of(context).textTheme.bodySmall,
        ),
        const SizedBox(height: 12),
        if (_loading) const LinearProgressIndicator(),
        if (_error != null) ...[
          Text(
            _error is ApiFailure
                ? (_error as ApiFailure).message
                : '无法加载管理功能，请更新服务器后重试',
          ),
          TextButton(onPressed: _load, child: const Text('重试')),
        ],
        if (_catalog != null)
          for (final group in _catalog!.groups.entries)
            Card(
              child: ListTile(
                leading: Icon(_icons[group.key] ?? Icons.settings),
                title: Text(group.value),
                subtitle: Text(
                  '${_catalog!.actions.where((a) => a['group'] == group.key).length} 项管理功能',
                ),
                trailing: const Icon(Icons.chevron_right),
                onTap: () => Navigator.of(context).push(
                  MaterialPageRoute<void>(
                    builder: (_) => AdminGroup(
                      api: widget.api,
                      catalog: _catalog!,
                      group: group.key,
                    ),
                  ),
                ),
              ),
            ),
      ],
    ),
  );
}

class AdminGroup extends StatefulWidget {
  final LibraryApi api;
  final AdminCatalog catalog;
  final String group;
  const AdminGroup({
    super.key,
    required this.api,
    required this.catalog,
    required this.group,
  });
  @override
  State<AdminGroup> createState() => _AdminGroupState();
}

class _AdminGroupState extends State<AdminGroup> {
  String _query = '';
  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: Text(widget.catalog.groups[widget.group]!)),
    body: ListView(
      padding: const EdgeInsets.all(16),
      children: [
        TextField(
          decoration: const InputDecoration(
            prefixIcon: Icon(Icons.search),
            labelText: '查找管理功能',
          ),
          onChanged: (v) => setState(() => _query = v.trim()),
        ),
        const SizedBox(height: 12),
        for (final action in widget.catalog.actions.where(
          (a) =>
              a['group'] == widget.group &&
              (a['title'] as String).contains(_query),
        ))
          ListTile(
            title: Text(action['title'] as String),
            leading: Icon(
              action['method'] == 'GET'
                  ? Icons.list_alt
                  : action['method'] == 'DELETE'
                  ? Icons.delete_outline
                  : Icons.edit_outlined,
            ),
            trailing: const Icon(Icons.chevron_right),
            onTap: () =>
                openAdminAction(context, widget.api, widget.catalog, action),
          ),
      ],
    ),
  );
}

Future<void> openAdminAction(
  BuildContext context,
  LibraryApi api,
  AdminCatalog catalog,
  JsonMap action, [
  JsonMap initial = const {},
]) => Navigator.of(context).push<void>(
  MaterialPageRoute(
    builder: (_) => AdminOperationPage(
      api: api,
      catalog: catalog,
      action: action,
      initial: initial,
    ),
  ),
);

class AdminFile {
  final String name, path;
  final int size;
  const AdminFile(this.name, this.path, this.size);
}

dynamic adminDefault(JsonMap schema, {bool required = false}) {
  if (schema.containsKey('default')) return schema['default'];
  if (schema['const'] == true) {
    return false; // External sends require explicit consent.
  }
  if (schema['type'] == 'object' && required) {
    return <String, dynamic>{
      for (final e in ((schema['properties'] as Map?) ?? {}).entries)
        e.key as String: adminDefault(
          e.value as JsonMap,
          required: ((schema['required'] as List?) ?? []).contains(e.key),
        ),
    };
  }
  if (schema['type'] == 'array' && required) return <dynamic>[];
  return null;
}

dynamic adminClean(dynamic value) => value is Map
    ? <String, dynamic>{
        for (final e in value.entries)
          if (e.value != null) e.key as String: adminClean(e.value),
      }
    : value is List
    ? value.map(adminClean).toList()
    : value;

class AdminOperationPage extends StatefulWidget {
  final LibraryApi api;
  final AdminCatalog catalog;
  final JsonMap action, initial;
  const AdminOperationPage({
    super.key,
    required this.api,
    required this.catalog,
    required this.action,
    this.initial = const {},
  });
  @override
  State<AdminOperationPage> createState() => _AdminOperationPageState();
}

class _AdminOperationPageState extends State<AdminOperationPage> {
  final _form = GlobalKey<FormState>();
  final JsonMap _parameters = {};
  dynamic _body, _result;
  bool _busy = false, _prefilling = false;
  String? _loadedPath;
  String? _error, _notice;
  int _revision = 0;
  List<JsonMap> get _fields =>
      (widget.action['parameters'] as List).cast<JsonMap>();
  @override
  void initState() {
    super.initState();
    for (final field in _fields) {
      _parameters[field['name'] as String] =
          widget.initial[field['name']] ??
          adminDefault(
            field['schema'] as JsonMap,
            required: field['required'] == true,
          );
    }
    if (widget.action['body'] is Map) {
      _body = adminDefault(widget.action['body'] as JsonMap, required: true);
      if (_body is Map) {
        for (final key in (_body as Map).keys.toList()) {
          if (widget.initial.containsKey(key)) _body[key] = widget.initial[key];
        }
      }
    }
    if (widget.action['method'] == 'GET' &&
        !_fields.any(
          (p) => p['required'] == true && _parameters[p['name']] == null,
        )) {
      WidgetsBinding.instance.addPostFrameCallback((_) => _execute());
    } else {
      _prefill();
    }
  }

  String? _path() {
    var path = widget.action['path'] as String;
    for (final parameter in _fields.where((p) => p['location'] == 'path')) {
      final value = _parameters[parameter['name']];
      if (value == null || value.toString().isEmpty) return null;
      path = path.replaceAll(
        '{${parameter['name']}}',
        Uri.encodeComponent(value.toString()),
      );
    }
    return path;
  }

  Future<void> _prefill() async {
    if (widget.action['method'] != 'PUT' || _body is! Map) return;
    final path = _path();
    if (path == null) return;
    final source = path.startsWith('/platform-endpoints/')
        ? '/platform-endpoints'
        : path.startsWith('/queue/lanes/')
        ? '/queue/status'
        : path;
    if (!widget.catalog.actions.any(
      (a) =>
          a['method'] == 'GET' &&
          (a['path'] == source || a['path'] == widget.action['path']),
    )) {
      return;
    }
    setState(() {
      _prefilling = true;
      _error = null;
      _loadedPath = null;
    });
    try {
      dynamic existing = await widget.api.get(source);
      if (path.startsWith('/platform-endpoints/')) {
        existing = (existing['items'] as List).firstWhere(
          (row) => row['platform'] == _parameters['platform'],
        );
      }
      if (path.startsWith('/queue/lanes/')) {
        final lanes = existing['lanes'];
        existing = lanes is List
            ? lanes.firstWhere((row) => row['id'] == _parameters['lane'])
            : lanes[_parameters['lane']];
      }
      if (!mounted) return;
      final schema = widget.action['body'] as JsonMap;
      final keys = (schema['properties'] as Map).keys;
      setState(() {
        _body = <String, dynamic>{
          for (final key in keys) key as String: existing[key] ?? _body[key],
        };
        _revision++;
        _loadedPath = path;
      });
    } catch (_) {
      if (mounted) setState(() => _error = '未能读取当前设置。请重试读取，确认内容后再保存。');
    } finally {
      if (mounted) setState(() => _prefilling = false);
    }
  }

  Future<bool> _confirm() async {
    if (widget.action['confirmation'] != true) return true;
    return await showDialog<bool>(
          context: context,
          builder: (context) => AlertDialog(
            title: Text('${widget.action['title']}？'),
            content: const Text('此操作会修改当前服务器的数据或运行状态，请确认所选内容。'),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('取消'),
              ),
              FilledButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('确认执行'),
              ),
            ],
          ),
        ) ??
        false;
  }

  Future<void> _execute() async {
    if (_busy || _prefilling || !(_form.currentState?.validate() ?? false)) {
      return;
    }
    final path = _path();
    if (widget.action['method'] == 'PUT' && path != null) {
      final source = path.startsWith('/platform-endpoints/')
          ? '/platform-endpoints'
          : path.startsWith('/queue/lanes/')
          ? '/queue/status'
          : path;
      if (widget.catalog.actions.any(
            (a) =>
                a['method'] == 'GET' &&
                (a['path'] == source || a['path'] == widget.action['path']),
          ) &&
          _loadedPath != path) {
        setState(() => _error = '请先读取当前设置，确认后再保存');
        return;
      }
    }
    if (path == null || !await _confirm() || !mounted) return;
    setState(() {
      _busy = true;
      _error = null;
      _notice = null;
    });
    final query = <String, String>{
      for (final f in _fields.where((p) => p['location'] == 'query'))
        if (_parameters[f['name']] != null)
          f['name'] as String: _parameters[f['name']].toString(),
    };
    try {
      dynamic result;
      if (widget.action['download'] == true) {
        final bytes = await widget.api.managementDownload(
          widget.action['method'] as String,
          path,
          query: query,
        );
        if (!mounted) return;
        final name = path == '/database/export'
            ? 'qiye-database.sql'
            : 'qiye-work.zip';
        final saved = await FilePicker.platform.saveFile(
          dialogTitle: '保存导出文件',
          fileName: name,
          bytes: bytes,
        );
        if (!mounted) return;
        _notice = saved == null ? '已取消保存' : '导出文件已保存到所选位置';
      } else if ((widget.action['media_type'] as String?)?.startsWith(
                'multipart/',
              ) ==
              true ||
          widget.action['media_type'] ==
              'application/vnd.android.package-archive') {
        final files = <({String field, String name, String path})>[];
        final fields = <String, String>{};
        if (_body is AdminFile) {
          final file = _body as AdminFile;
          files.add((field: 'file', name: file.name, path: file.path));
        } else if (_body is Map) {
          for (final entry in (_body as Map).entries) {
            final values = entry.value is List
                ? entry.value as List
                : [entry.value];
            for (final value in values) {
              if (value is AdminFile) {
                files.add((
                  field: entry.key as String,
                  name: value.name,
                  path: value.path,
                ));
              }
            }
            if (entry.value != null && !values.any((v) => v is AdminFile)) {
              fields[entry.key as String] = entry.value.toString();
            }
          }
        }
        result = await widget.api.managementUpload(
          widget.action['method'] as String,
          path,
          query: query,
          files: files,
          fields: fields,
          raw:
              widget.action['media_type'] ==
              'application/vnd.android.package-archive',
        );
      } else {
        result = await widget.api.manage(
          widget.action['method'] as String,
          path,
          query: query,
          body: _body is Map ? (adminClean(_body) as JsonMap) : null,
        );
      }
      if (mounted) {
        setState(() {
          _result = result;
          _notice ??= widget.action['method'] == 'GET' ? null : '操作已完成';
        });
      }
    } catch (error) {
      if (mounted) {
        setState(
          () => _error = error is ApiFailure
              ? error.message
              : '操作未完成，请检查输入和网络后重试',
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => PopScope(
    canPop: !_busy,
    child: Scaffold(
      appBar: AppBar(title: Text(widget.action['title'] as String)),
      body: Form(
        key: _form,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            for (final field in _fields)
              AdminField(
                key: ValueKey('$_revision:parameter:${field['name']}'),
                name: field['name'] as String,
                schema: field['schema'] as JsonMap,
                required: field['required'] == true,
                value: _parameters[field['name']],
                api: widget.api,
                catalog: widget.catalog,
                resource: widget.action['path'] as String,
                values: _parameters,
                enabled: !_busy && !_prefilling,
                onChanged: (v) {
                  setState(() => _parameters[field['name'] as String] = v);
                  if (field['location'] == 'path') unawaited(_prefill());
                },
              ),
            if (widget.action['method'] == 'PUT' && _body is Map)
              TextButton.icon(
                onPressed: _busy || _prefilling ? null : _prefill,
                icon: const Icon(Icons.refresh),
                label: const Text('读取当前设置'),
              ),
            if (_prefilling) const LinearProgressIndicator(),
            if (widget.action['body'] is Map)
              AdminField(
                key: ValueKey('$_revision:body'),
                name: '',
                schema: widget.action['body'] as JsonMap,
                required: true,
                value: _body,
                api: widget.api,
                catalog: widget.catalog,
                resource: widget.action['path'] as String,
                values: _parameters,
                enabled: !_busy && !_prefilling,
                onChanged: (v) => setState(() => _body = v),
              ),
            const SizedBox(height: 12),
            FilledButton.icon(
              onPressed: _busy || _prefilling ? null : _execute,
              icon: _busy
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : Icon(
                      widget.action['method'] == 'GET'
                          ? Icons.refresh
                          : Icons.check,
                    ),
              label: Text(
                _busy
                    ? '正在处理…'
                    : widget.action['method'] == 'GET'
                    ? '查询 / 刷新'
                    : widget.action['title'] as String,
              ),
            ),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 16),
                child: Text(
                  _error!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              ),
            if (_notice != null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 16),
                child: Text(_notice!),
              ),
            if (_result != null)
              AdminResult(
                value: _result,
                api: widget.api,
                catalog: widget.catalog,
                group: widget.action['group'] as String,
                resource: widget.action['path'] as String,
              ),
          ],
        ),
      ),
    ),
  );
}

class AdminField extends StatelessWidget {
  final String name;
  final JsonMap schema;
  final bool required, enabled;
  final dynamic value;
  final ValueChanged<dynamic> onChanged;
  final LibraryApi api;
  final AdminCatalog catalog;
  final String resource;
  final JsonMap values;
  const AdminField({
    super.key,
    required this.name,
    required this.schema,
    required this.required,
    required this.value,
    required this.onChanged,
    required this.api,
    required this.catalog,
    this.enabled = true,
    this.resource = '',
    this.values = const {},
  });
  String get label => '${catalog.label(name)}${required ? ' *' : ''}';
  Widget child(
    String key,
    JsonMap spec,
    dynamic item,
    ValueChanged<dynamic> change, {
    bool required = false,
  }) => AdminField(
    key: ValueKey(key),
    name: key,
    schema: spec,
    required: required,
    value: item,
    onChanged: change,
    api: api,
    catalog: catalog,
    enabled: enabled,
    resource: resource,
    values: values,
  );
  Future<void> _choose(BuildContext context, bool multiple) async {
    final selected = await showModalBottomSheet<dynamic>(
      context: context,
      isScrollControlled: true,
      builder: (_) => AdminEntityPicker(
        api: api,
        catalog: catalog,
        name: name,
        multiple: multiple,
        selected: multiple
            ? List<String>.from(value as List? ?? [])
            : value == null
            ? []
            : [value.toString()],
        resource: resource,
        platform: values['platform'] as String?,
      ),
    );
    if (selected != null) onChanged(selected);
  }

  bool get entity => {
    'account_id',
    'main_account_id',
    'content_account_id',
    'content_account_ids',
    'detail_account_ids',
    'check_account_ids',
    'item_id',
    'item_ids',
    'job_id',
    'device_id',
    'node_id',
    'node_ids',
    'subscription_id',
  }.contains(name);
  Future<dynamic> _editEntry(
    BuildContext context,
    JsonMap spec,
    dynamic initial,
  ) async {
    dynamic draft = initial;
    final form = GlobalKey<FormState>();
    return showDialog<dynamic>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, change) => AlertDialog(
          title: Text('编辑${catalog.label(name)}'),
          content: SizedBox(
            width: 500,
            child: SingleChildScrollView(
              child: Form(
                key: form,
                child: child(
                  catalog.label(name),
                  spec,
                  draft,
                  (v) => change(() => draft = v),
                  required: true,
                ),
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('取消'),
            ),
            FilledButton(
              onPressed: () {
                if (form.currentState!.validate()) {
                  Navigator.pop(context, draft);
                }
              },
              child: const Text('确定'),
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final type = schema['type'];
    if (schema['format'] == 'binary' ||
        type == 'array' &&
            (schema['items'] as JsonMap?)?['format'] == 'binary') {
      final selected = value is List
          ? value as List
          : value == null
          ? []
          : [value];
      return FormField<dynamic>(
        initialValue: value,
        validator: (_) => required && selected.isEmpty ? '请选择文件' : null,
        builder: (state) => Padding(
          padding: const EdgeInsets.symmetric(vertical: 8),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              OutlinedButton.icon(
                onPressed: !enabled
                    ? null
                    : () async {
                        try {
                          final picked = await FilePicker.platform.pickFiles(
                            allowMultiple: type == 'array',
                            withData: false,
                          );
                          if (picked == null || !context.mounted) return;
                          final files = picked.files
                              .where((f) => f.path != null)
                              .map((f) => AdminFile(f.name, f.path!, f.size))
                              .toList();
                          onChanged(
                            type == 'array'
                                ? files
                                : files.isEmpty
                                ? null
                                : files.first,
                          );
                        } catch (_) {
                          if (context.mounted) {
                            ScaffoldMessenger.of(context).showSnackBar(
                              const SnackBar(content: Text('无法打开系统文件选择器，请重试')),
                            );
                          }
                        }
                      },
                icon: const Icon(Icons.attach_file),
                label: Text(name.isEmpty ? '选择 APK 安装包' : label),
              ),
              for (final file in selected.cast<AdminFile>())
                Text(
                  '${file.name} · ${(file.size / 1024 / 1024).toStringAsFixed(1)} MB',
                ),
              if (state.errorText != null)
                Text(
                  state.errorText!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
            ],
          ),
        ),
      );
    }
    if (entity) {
      final multiple = type == 'array';
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(label),
            const SizedBox(height: 6),
            OutlinedButton.icon(
              onPressed: enabled ? () => _choose(context, multiple) : null,
              icon: const Icon(Icons.list),
              label: Text(
                multiple
                    ? '选择${catalog.label(name)}（${(value as List?)?.length ?? 0}）'
                    : value == null
                    ? '选择${catalog.label(name)}'
                    : '已选择：$value',
              ),
            ),
            if (!required && value != null)
              TextButton(
                onPressed: enabled ? () => onChanged(null) : null,
                child: const Text('不指定 / 保持默认'),
              ),
            FormField<dynamic>(
              validator: (_) =>
                  required &&
                      (value == null ||
                          value is List && (value as List).isEmpty)
                  ? '请选择${catalog.label(name)}'
                  : null,
              builder: (s) => s.errorText == null
                  ? const SizedBox.shrink()
                  : Text(
                      s.errorText!,
                      style: TextStyle(
                        color: Theme.of(context).colorScheme.error,
                      ),
                    ),
            ),
          ],
        ),
      );
    }
    if (type == 'object') {
      final current = Map<String, dynamic>.from(value as Map? ?? {});
      final properties = (schema['properties'] as JsonMap?) ?? {};
      final rows = <Widget>[];
      if (properties.isNotEmpty) {
        for (final entry in properties.entries) {
          rows.add(
            child(
              entry.key,
              entry.value as JsonMap,
              current[entry.key],
              (v) {
                current[entry.key] = v;
                onChanged(current);
              },
              required: ((schema['required'] as List?) ?? []).contains(
                entry.key,
              ),
            ),
          );
        }
      } else {
        final spec = schema['additionalProperties'] is Map
            ? <String, dynamic>{
                ...schema['additionalProperties'] as JsonMap,
                if (name == 'credentials') 'writeOnly': true,
              }
            : <String, dynamic>{'type': 'string'};
        for (final entry in current.entries) {
          rows.add(
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: child(entry.key, spec, entry.value, (v) {
                    current[entry.key] = v;
                    onChanged(current);
                  }),
                ),
                IconButton(
                  onPressed: enabled
                      ? () {
                          current.remove(entry.key);
                          onChanged(current);
                        }
                      : null,
                  icon: const Icon(Icons.remove_circle_outline),
                ),
              ],
            ),
          );
        }
        rows.add(
          TextButton.icon(
            onPressed: !enabled
                ? null
                : () async {
                    final controller = TextEditingController();
                    final key = await showDialog<String>(
                      context: context,
                      builder: (context) => AlertDialog(
                        title: Text(
                          name == 'account_limits' ? '输入账号编号' : '添加配置项',
                        ),
                        content: TextField(
                          controller: controller,
                          decoration: InputDecoration(
                            labelText: name == 'account_limits'
                                ? '账号编号'
                                : '配置名称',
                          ),
                        ),
                        actions: [
                          TextButton(
                            onPressed: () => Navigator.pop(context),
                            child: const Text('取消'),
                          ),
                          FilledButton(
                            onPressed: () {
                              if (controller.text.trim().isNotEmpty) {
                                Navigator.pop(context, controller.text.trim());
                              }
                            },
                            child: const Text('添加'),
                          ),
                        ],
                      ),
                    );
                    await Future<void>.delayed(
                      const Duration(milliseconds: 250),
                    );
                    controller.dispose();
                    if (key != null) {
                      current[key] = spec['type'] == 'integer' ? 1 : '';
                      onChanged(current);
                    }
                  },
            icon: const Icon(Icons.add),
            label: Text(name == 'account_limits' ? '添加账号并发设置' : '添加配置项'),
          ),
        );
      }
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (name.isNotEmpty)
              Text(label, style: Theme.of(context).textTheme.titleSmall),
            ...rows,
          ],
        ),
      );
    }
    if (type == 'array') {
      final items = List<dynamic>.from(value as List? ?? []);
      final spec = (schema['items'] as JsonMap?) ?? {'type': 'string'};
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(label, style: Theme.of(context).textTheme.titleSmall),
            for (var i = 0; i < items.length; i++)
              ListTile(
                title: Text(
                  items[i] is Map
                      ? (items[i]['label'] ??
                                items[i]['item_id'] ??
                                '第 ${i + 1} 项')
                            .toString()
                      : (items[i] ?? '').toString(),
                ),
                leading: const Icon(Icons.edit_outlined),
                onTap: !enabled
                    ? null
                    : () async {
                        final edited = await _editEntry(
                          context,
                          spec,
                          items[i],
                        );
                        if (edited != null) {
                          items[i] = edited;
                          onChanged(items);
                        }
                      },
                trailing: IconButton(
                  onPressed: !enabled
                      ? null
                      : () {
                          items.removeAt(i);
                          onChanged(items);
                        },
                  icon: const Icon(Icons.remove_circle_outline),
                ),
              ),
            TextButton.icon(
              onPressed:
                  !enabled ||
                      items.length >=
                          ((schema['maxItems'] as num?)?.toInt() ?? 2000)
                  ? null
                  : () async {
                      final edited = await _editEntry(
                        context,
                        spec,
                        adminDefault(spec, required: true),
                      );
                      if (edited != null) {
                        items.add(edited);
                        onChanged(items);
                      }
                    },
              icon: const Icon(Icons.add),
              label: const Text('添加一项'),
            ),
            if (items.isEmpty && value != null)
              TextButton(
                onPressed: enabled ? () => onChanged(null) : null,
                child: const Text('保持不变'),
              ),
            FormField<dynamic>(
              validator: (_) =>
                  required &&
                      items.length <
                          ((schema['minItems'] as num?)?.toInt() ?? 0)
                  ? '请选择或添加足够的内容'
                  : null,
              builder: (s) => s.errorText == null
                  ? const SizedBox.shrink()
                  : Text(s.errorText!),
            ),
          ],
        ),
      );
    }
    if (type == 'boolean' || schema['const'] is bool) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: DropdownButtonFormField<bool>(
          key: ValueKey('$name:$value'),
          initialValue: value as bool?,
          isExpanded: true,
          decoration: InputDecoration(labelText: label),
          items: [
            if (!required)
              const DropdownMenuItem<bool>(
                value: null,
                child: Text('保持不变 / 使用默认'),
              ),
            DropdownMenuItem(
              value: true,
              child: Text(name == 'consent' ? '同意' : '是'),
            ),
            DropdownMenuItem(
              value: false,
              child: Text(name == 'consent' ? '不同意' : '否'),
            ),
          ],
          onChanged: enabled ? onChanged : null,
          validator: (v) => required && v == null
              ? '请选择'
              : schema['const'] == true && v != true
              ? '需要明确同意后才能发送'
              : null,
        ),
      );
    }
    if (schema['enum'] is List) {
      final choices = schema['enum'] as List;
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: DropdownButtonFormField<dynamic>(
          key: ValueKey('$name:$value'),
          initialValue: choices.contains(value) ? value : null,
          isExpanded: true,
          decoration: InputDecoration(labelText: label),
          items: [
            if (!required)
              const DropdownMenuItem(value: null, child: Text('不限制 / 使用默认')),
            for (final option in choices)
              DropdownMenuItem(
                value: option,
                child: Text(adminChoice(option.toString())),
              ),
          ],
          onChanged: enabled ? onChanged : null,
          validator: (v) => required && v == null ? '请选择$label' : null,
        ),
      );
    }
    final numeric = type == 'integer' || type == 'number';
    final secret =
        schema['writeOnly'] == true ||
        schema['format'] == 'password' ||
        RegExp('password|secret|api_key|token|code').hasMatch(name);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: TextFormField(
        initialValue: value?.toString() ?? '',
        enabled: enabled,
        obscureText: secret,
        autocorrect: !secret && !numeric,
        enableSuggestions: !secret,
        maxLines: secret
            ? 1
            : name == 'description' || name == 'notes'
            ? 5
            : 1,
        keyboardType: numeric
            ? const TextInputType.numberWithOptions(decimal: true, signed: true)
            : TextInputType.text,
        decoration: InputDecoration(
          labelText: label,
          helperText:
              numeric &&
                  (schema['minimum'] != null || schema['maximum'] != null)
              ? '范围 ${schema['minimum'] ?? '不限'} ～ ${schema['maximum'] ?? '不限'}'
              : null,
        ),
        onChanged: (v) => onChanged(
          numeric
              ? (v.isEmpty
                    ? null
                    : type == 'integer'
                    ? int.tryParse(v)
                    : double.tryParse(v))
              : v,
        ),
        validator: (v) {
          if (required && (v == null || v.isEmpty)) {
            return '请填写${catalog.label(name)}';
          }
          if (v == null || v.isEmpty) return null;
          if (v.length < ((schema['minLength'] as num?)?.toInt() ?? 0) ||
              v.length > ((schema['maxLength'] as num?)?.toInt() ?? 100000)) {
            return '长度不符合要求';
          }
          if (numeric) {
            final n = type == 'integer' ? int.tryParse(v) : double.tryParse(v);
            if (n == null || !n.isFinite) {
              return '请输入有效${type == 'integer' ? '整数' : '数字'}';
            }
            if (schema['minimum'] is num && n < (schema['minimum'] as num) ||
                schema['maximum'] is num && n > (schema['maximum'] as num)) {
              return '超出允许范围';
            }
          }
          return null;
        },
      ),
    );
  }
}

String adminChoice(String value) =>
    librarySources[value] ??
    const {
      'author': '作者',
      'tag': '标签',
      'search': '关键词搜索',
      'all': '全部',
      'images': '漫画和插画',
      'novels': '小说',
      'manga': '漫画',
      'illustration': '插画',
      'novel': '小说',
      'article': '文章',
      'general': '全年龄',
      'sensitive': '敏感',
      'adult': 'R18',
      'unknown': '未分类',
      'download': '内容下载',
      'details': '作品详情',
      'checks': '作者与订阅检查',
      'ai': 'AI 识别',
      'translation': 'AI 汉化',
      'other': '其他',
      'ollama': '本地 Ollama',
      'openai': '兼容 OpenAI 的模型接口',
      'queued': '等待中',
      'running': '运行中',
      'completed': '已完成',
      'failed': '失败',
      'cancelled': '已取消',
      'latest': '最新添加',
      'oldest': '最早添加',
      'title': '标题',
      'active': '有效',
      'revoked': '已撤销',
      'expired': '已过期',
    }[value] ??
    value;

class AdminEntityPicker extends StatefulWidget {
  final LibraryApi api;
  final AdminCatalog catalog;
  final String name;
  final bool multiple;
  final List<String> selected;
  final String resource;
  final String? platform;
  const AdminEntityPicker({
    super.key,
    required this.api,
    required this.catalog,
    required this.name,
    required this.multiple,
    required this.selected,
    this.resource = '',
    this.platform,
  });
  @override
  State<AdminEntityPicker> createState() => _AdminEntityPickerState();
}

class _AdminEntityPickerState extends State<AdminEntityPicker> {
  late final Set<String> _selected = {...widget.selected};
  List<JsonMap> _rows = [];
  bool _busy = true;
  String? _error;
  final _search = TextEditingController();
  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final name = widget.name;
      dynamic data;
      List<JsonMap> rows;
      if (name.contains('account')) {
        data = await widget.api.get('/sources');
        rows = [
          for (final platform in (data as List).where(
            (p) => widget.platform == null || p['platform'] == widget.platform,
          ))
            for (final account in platform['accounts'] as List)
              {
                ...account as JsonMap,
                'display':
                    '${adminChoice(platform['platform'] as String)} · ${account['label']}',
              },
        ];
      } else if (name.startsWith('node')) {
        data = await widget.api.get('/network/managed');
        rows = (data['nodes'] as List).cast<JsonMap>();
      } else if (name == 'subscription_id' &&
          widget.resource.startsWith('/network/')) {
        data = await widget.api.get('/network/managed');
        rows = (data['subscriptions'] as List).cast<JsonMap>();
      } else {
        final path = name.startsWith('job')
            ? '/jobs'
            : name.startsWith('device')
            ? '/devices'
            : name.startsWith('subscription')
            ? '/subscriptions'
            : '/items';
        data = await widget.api.get(
          path,
          query: path == '/items'
              ? {'q': _search.text, 'page_size': '100'}
              : null,
        );
        rows = (data is List ? data : data['items'] as List).cast<JsonMap>();
      }
      if (mounted) {
        setState(
          () => _rows = rows
              .where(
                (r) =>
                    _search.text.isEmpty ||
                    r.toString().toLowerCase().contains(
                      _search.text.toLowerCase(),
                    ),
              )
              .toList(),
        );
      }
    } catch (_) {
      if (mounted) setState(() => _error = '无法加载列表，请检查连接后重试');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => SafeArea(
    child: SizedBox(
      height: MediaQuery.sizeOf(context).height * .85,
      child: Column(
        children: [
          ListTile(
            title: Text('选择${widget.catalog.label(widget.name)}'),
            trailing: widget.multiple
                ? TextButton(
                    onPressed: () => Navigator.pop(context, _selected.toList()),
                    child: const Text('完成'),
                  )
                : IconButton(
                    onPressed: () => Navigator.pop(context),
                    icon: const Icon(Icons.close),
                  ),
          ),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: TextField(
              controller: _search,
              onSubmitted: (_) => _load(),
              decoration: InputDecoration(
                labelText: '搜索列表',
                suffixIcon: IconButton(
                  onPressed: _busy ? null : _load,
                  icon: const Icon(Icons.search),
                ),
              ),
            ),
          ),
          if (_busy) const LinearProgressIndicator(),
          if (_error != null)
            TextButton(onPressed: _load, child: Text(_error!)),
          Expanded(
            child: ListView.builder(
              itemCount: _rows.length,
              itemBuilder: (_, i) {
                final row = _rows[i], id = _rows[i]['id'].toString();
                final title =
                    row['display'] ??
                    row['label'] ??
                    row['title_zh'] ??
                    row['title'] ??
                    row['name'] ??
                    row['device_name'] ??
                    row['kind'] ??
                    id;
                return CheckboxListTile(
                  title: Text(adminChoice(title.toString())),
                  subtitle: Text(id),
                  value: _selected.contains(id),
                  onChanged: (_) {
                    if (!widget.multiple) {
                      Navigator.pop(context, id);
                      return;
                    }
                    setState(() {
                      if (!_selected.add(id)) _selected.remove(id);
                    });
                  },
                );
              },
            ),
          ),
          Padding(
            padding: const EdgeInsets.all(16),
            child: OutlinedButton(
              onPressed: () async {
                final controller = TextEditingController();
                final id = await showDialog<String>(
                  context: context,
                  builder: (context) => AlertDialog(
                    title: const Text('输入编号'),
                    content: TextField(controller: controller),
                    actions: [
                      TextButton(
                        onPressed: () => Navigator.pop(context),
                        child: const Text('取消'),
                      ),
                      FilledButton(
                        onPressed: () {
                          if (controller.text.trim().isNotEmpty) {
                            Navigator.pop(context, controller.text.trim());
                          }
                        },
                        child: const Text('确定'),
                      ),
                    ],
                  ),
                );
                await Future<void>.delayed(const Duration(milliseconds: 250));
                controller.dispose();
                if (id == null || !context.mounted) return;
                if (widget.multiple) {
                  setState(() => _selected.add(id));
                } else {
                  Navigator.pop(context, id);
                }
              },
              child: const Text('按编号选择（列表之外的内容）'),
            ),
          ),
        ],
      ),
    ),
  );
}

class AdminResult extends StatefulWidget {
  final dynamic value;
  final LibraryApi api;
  final AdminCatalog catalog;
  final String group;
  final String name;
  final int depth;
  final String resource;
  final JsonMap scope;
  const AdminResult({
    super.key,
    required this.value,
    required this.api,
    required this.catalog,
    required this.group,
    this.name = '',
    this.depth = 0,
    this.resource = '',
    this.scope = const {},
  });
  @override
  State<AdminResult> createState() => _AdminResultState();
}

class _AdminResultState extends State<AdminResult> {
  int _limit = 30;
  Widget _child(dynamic value, String name, [JsonMap? scope]) => AdminResult(
    value: value,
    api: widget.api,
    catalog: widget.catalog,
    group: widget.group,
    name: name,
    depth: widget.depth + 1,
    resource: widget.resource,
    scope: scope ?? widget.scope,
  );
  Future<void> _actions(JsonMap row) async {
    final contextValues = _rowContext(row);
    final actions = widget.catalog.actions
        .where(
          (a) =>
              a['group'] == widget.group &&
              ((a['parameters'] as List).any((p) => p['location'] == 'path') ||
                  (a['body'] is Map &&
                      ((a['body']['properties'] as Map?) ?? {}).keys.any(
                        (key) => key == 'node_id' && contextValues[key] != null,
                      ))) &&
              (a['parameters'] as List)
                  .where((p) => p['location'] == 'path')
                  .every((p) => contextValues[p['name']] != null),
        )
        .toList();
    if (actions.isEmpty) return;
    final selected = await showModalBottomSheet<JsonMap>(
      context: context,
      builder: (context) => SafeArea(
        child: ListView(
          shrinkWrap: true,
          children: [
            for (final action in actions)
              ListTile(
                title: Text(action['title'] as String),
                onTap: () => Navigator.pop(context, action),
              ),
          ],
        ),
      ),
    );
    if (selected != null && mounted) {
      await openAdminAction(
        context,
        widget.api,
        widget.catalog,
        selected,
        contextValues,
      );
    }
  }

  JsonMap _rowContext(JsonMap row) {
    final contextValues = <String, dynamic>{...widget.scope, ...row};
    final id = row['id'];
    if (id != null) {
      if (widget.group == 'queue') {
        if (widget.name == 'lanes') {
          contextValues['lane'] = id;
        } else {
          contextValues['job_id'] = id;
        }
      }
      if (widget.group == 'subscriptions' ||
          widget.group == 'network' && widget.name == 'subscriptions') {
        contextValues['subscription_id'] = id;
      }
      if (widget.group == 'devices') contextValues['device_id'] = id;
      if (widget.group == 'library') {
        contextValues[widget.resource.startsWith('/series')
                ? 'series_id'
                : widget.resource.startsWith('/characters')
                ? 'tag_id'
                : widget.resource.startsWith('/pixiv/authors')
                ? 'author_id'
                : widget.resource.startsWith('/favorite-groups')
                ? 'group_id'
                : 'item_id'] =
            id;
      }
      if (widget.group == 'sources') contextValues['account_id'] = id;
      if (widget.name == 'nodes') contextValues['node_id'] = id;
    }
    return contextValues;
  }

  @override
  Widget build(BuildContext context) {
    final value = widget.value;
    if (value == null) return const SizedBox.shrink();
    if (widget.depth > 8) {
      return SelectableText(
        '${widget.catalog.label(widget.name)}：${jsonEncode(value)}',
      );
    }
    if (value is List) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (widget.name.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 12),
              child: Text(
                '${widget.catalog.label(widget.name)}（${value.length}）',
                style: Theme.of(context).textTheme.titleSmall,
              ),
            ),
          for (var i = 0; i < value.length && i < _limit; i++)
            _child(value[i], widget.name),
          if (value.length > _limit)
            TextButton(
              onPressed: () => setState(() => _limit += 30),
              child: Text('继续显示（剩余 ${value.length - _limit} 项）'),
            ),
          if (value.isEmpty)
            const Padding(padding: EdgeInsets.all(12), child: Text('暂无内容')),
        ],
      );
    }
    if (value is Map) {
      final row = Map<String, dynamic>.from(value);
      final title =
          row['label'] ??
          row['title_zh'] ??
          row['title'] ??
          row['name'] ??
          row['device_name'] ??
          row['platform'] ??
          row['kind'] ??
          (widget.name.isEmpty ? '当前状态' : widget.catalog.label(widget.name));
      return Card(
        child: ExpansionTile(
          maintainState: true,
          title: Text(adminChoice(title.toString())),
          subtitle: row['status'] == null
              ? null
              : Text(adminChoice(row['status'].toString())),
          childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
          children: [
            if (row['id'] != null || row['platform'] != null)
              Align(
                alignment: Alignment.centerLeft,
                child: TextButton.icon(
                  onPressed: () => _actions(row),
                  icon: const Icon(Icons.tune),
                  label: const Text('相关管理操作'),
                ),
              ),
            for (final e in row.entries)
              _child(e.value, e.key, _rowContext(row)),
          ],
        ),
      );
    }
    final text = value is bool
        ? (value ? '是' : '否')
        : adminChoice(value.toString());
    final uri = Uri.tryParse(value.toString());
    final link =
        uri != null &&
        uri.scheme == 'https' &&
        uri.host.isNotEmpty &&
        uri.userInfo.isEmpty;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 5),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (widget.name.isNotEmpty)
            Text(
              widget.catalog.label(widget.name),
              style: Theme.of(context).textTheme.labelMedium,
            ),
          SelectableText(text),
          if (widget.name == 'qr_payload' ||
              widget.name == 'id' ||
              widget.name == 'session_id')
            TextButton(
              onPressed: () async {
                await Clipboard.setData(ClipboardData(text: value.toString()));
                if (context.mounted) {
                  ScaffoldMessenger.of(
                    context,
                  ).showSnackBar(const SnackBar(content: Text('已复制')));
                }
              },
              child: const Text('复制'),
            ),
          if (link)
            TextButton(
              onPressed: () async {
                final accepted = await showDialog<bool>(
                  context: context,
                  builder: (context) => AlertDialog(
                    title: const Text('打开外部网页'),
                    content: Text('即将打开 ${uri.host}'),
                    actions: [
                      TextButton(
                        onPressed: () => Navigator.pop(context, false),
                        child: const Text('取消'),
                      ),
                      FilledButton(
                        onPressed: () => Navigator.pop(context, true),
                        child: const Text('打开'),
                      ),
                    ],
                  ),
                );
                if (accepted == true) {
                  try {
                    if (!await launchUrl(
                      uri,
                      mode: LaunchMode.externalApplication,
                    )) {
                      throw StateError('no browser');
                    }
                  } catch (_) {
                    if (context.mounted) {
                      ScaffoldMessenger.of(context).showSnackBar(
                        const SnackBar(content: Text('无法打开浏览器，可选择复制链接后手动打开')),
                      );
                    }
                  }
                }
              },
              child: const Text('在浏览器打开'),
            ),
        ],
      ),
    );
  }
}
