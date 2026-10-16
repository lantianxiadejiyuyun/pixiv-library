import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'api.dart';
import 'binding_screen.dart';
import 'library_screen.dart';
import 'session.dart';
import 'app_update.dart';
import 'image_cache_card.dart';
import 'platform_endpoints_screen.dart';
import 'admin_screen.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  // Keep decoded manga pages bounded in memory on entry-level phones.
  PaintingBinding.instance.imageCache.maximumSizeBytes = 96 * 1024 * 1024;
  runApp(QiyeApp(store: SecureSessionStore()));
}

class QiyeApp extends StatefulWidget {
  final SessionStore store;
  const QiyeApp({super.key, required this.store});
  @override
  State<QiyeApp> createState() => _QiyeAppState();
}

class _QiyeAppState extends State<QiyeApp> {
  final _navigator = GlobalKey<NavigatorState>();
  DeviceSession? _session;
  LibraryApi? _api;
  bool _loading = true, _clearing = false;
  String? _notice, _storageError;
  @override
  void initState() {
    super.initState();
    _restore();
  }

  @override
  void dispose() {
    _api?.close();
    super.dispose();
  }

  Future<void> _restore() async {
    setState(() {
      _loading = true;
      _storageError = null;
    });
    try {
      final session = await widget.store.read();
      if (!mounted) return;
      if (session != null) _connected(session);
    } catch (_) {
      if (mounted) {
        setState(() {
          _storageError = '无法读取安全存储，请解锁设备后重试';
        });
      }
    } finally {
      if (mounted) {
        setState(() {
          _loading = false;
        });
      }
    }
  }

  void _connected(DeviceSession session) {
    _api?.close();
    setState(() {
      _session = session;
      _notice = null;
      _api = LibraryApi.forSession(
        session,
        onUnauthorized: () {
          if (_session?.deviceId == session.deviceId) {
            _clear('设备授权已失效，请重新扫码绑定');
          }
        },
      );
    });
  }

  Future<bool> _clear(String notice) async {
    if (_clearing) return false;
    _clearing = true;
    try {
      try {
        await _api?.purgeImageCache();
      } catch (_) {
        // A storage failure must not prevent revoking the local session.
      }
      PaintingBinding.instance.imageCache.clear();
      PaintingBinding.instance.imageCache.clearLiveImages();
      await widget.store.clear();
      if (!mounted) return false;
      _navigator.currentState?.popUntil((route) => route.isFirst);
      _api?.close();
      setState(() {
        _session = null;
        _api = null;
        _notice = notice;
      });
      return true;
    } catch (_) {
      if (mounted) {
        _navigator.currentState?.popUntil((route) => route.isFirst);
        _api?.close();
        setState(() {
          _session = null;
          _api = null;
          _storageError = '无法清除系统凭据，请在网页撤销此设备后重试';
        });
      }
      return false;
    } finally {
      _clearing = false;
    }
  }

  void _settings() {
    final session = _session!, api = _api!;
    _navigator.currentState!.push(
      MaterialPageRoute(
        builder: (_) =>
            ServerScreen(session: session, api: api, onDisconnect: _clear),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    ThemeData theme(Brightness brightness) => ThemeData(
      useMaterial3: true,
      brightness: brightness,
      colorScheme: ColorScheme.fromSeed(
        seedColor: const Color(0xff5967aa),
        brightness: brightness,
      ),
      appBarTheme: const AppBarTheme(centerTitle: false),
      inputDecorationTheme: const InputDecorationTheme(
        border: OutlineInputBorder(),
      ),
    );
    return MaterialApp(
      title: '栖页',
      debugShowCheckedModeBanner: false,
      navigatorKey: _navigator,
      theme: theme(Brightness.light),
      darkTheme: theme(Brightness.dark),
      themeMode: ThemeMode.system,
      locale: const Locale('zh', 'CN'),
      supportedLocales: const [Locale('zh', 'CN'), Locale('en')],
      localizationsDelegates: GlobalMaterialLocalizations.delegates,
      home: _loading
          ? const Scaffold(body: Center(child: CircularProgressIndicator()))
          : _storageError != null
          ? Scaffold(
              body: Center(
                child: Padding(
                  padding: const EdgeInsets.all(28),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text(_storageError!),
                      const SizedBox(height: 16),
                      FilledButton(
                        onPressed: _restore,
                        child: const Text('重试'),
                      ),
                    ],
                  ),
                ),
              ),
            )
          : _session == null
          ? BindingScreen(
              pairing: PairingService(widget.store),
              onBound: _connected,
              notice: _notice,
            )
          : LibraryScreen(
              key: ValueKey(_session!.deviceId),
              api: _api!,
              onSettings: _settings,
            ),
    );
  }
}

class ServerScreen extends StatefulWidget {
  final DeviceSession session;
  final LibraryApi api;
  final Future<bool> Function(String notice) onDisconnect;
  const ServerScreen({
    super.key,
    required this.session,
    required this.api,
    required this.onDisconnect,
  });
  @override
  State<ServerScreen> createState() => _ServerScreenState();
}

class _ServerScreenState extends State<ServerScreen> {
  bool _busy = false;
  String? _status;
  Future<void> _check() async {
    setState(() {
      _busy = true;
      _status = null;
    });
    try {
      await widget.api.get('/dashboard');
      if (mounted) {
        setState(() {
          _status = '连接正常';
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _status = e.toString();
        });
      }
    } finally {
      if (mounted) {
        setState(() {
          _busy = false;
        });
      }
    }
  }

  Future<void> _disconnect() async {
    setState(() {
      _busy = true;
    });
    var notice = '已解除服务器绑定';
    try {
      await widget.api.post('/devices/disconnect');
    } catch (_) {
      notice = '已清除本机绑定。服务器暂不可达，请在网页撤销旧设备。';
    }
    await widget.onDisconnect(notice);
    if (mounted) {
      setState(() {
        _busy = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('服务器')),
    body: ListView(
      padding: const EdgeInsets.all(20),
      children: [
        const Icon(Icons.dns_rounded, size: 48),
        const SizedBox(height: 24),
        ListTile(
          title: const Text('地址'),
          subtitle: SelectableText(widget.session.server.toString()),
        ),
        ListTile(
          title: const Text('设备名称'),
          subtitle: Text(widget.session.deviceName),
        ),
        ListTile(
          title: const Text('授权到期'),
          subtitle: Text(
            widget.session.expiresAt.toLocal().toString().split('.').first,
          ),
        ),
        const ListTile(
          title: Text('阅读权限'),
          subtitle: Text('浏览书库、阅读内容、保存进度、请求服务器下载正文'),
        ),
        const Divider(),
        const SizedBox(height: 16),
        OutlinedButton.icon(
          onPressed: _busy ? null : _check,
          icon: const Icon(Icons.wifi_tethering),
          label: const Text('检查连接'),
        ),
        if (_status != null)
          Padding(padding: const EdgeInsets.all(12), child: Text(_status!)),
        const SizedBox(height: 16),
        OutlinedButton.icon(
          onPressed: _busy ? null : _disconnect,
          icon: const Icon(Icons.link_off),
          label: const Text('解除绑定 / 更换服务器'),
        ),
        if (_busy)
          const Padding(
            padding: EdgeInsets.all(16),
            child: LinearProgressIndicator(),
          ),
        const SizedBox(height: 28),
        Card(
          child: ListTile(
            leading: const Icon(Icons.admin_panel_settings_outlined),
            title: const Text('后台管理'),
            subtitle: const Text('管理员登录，管理账号、队列和服务器'),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute<void>(
                builder: (_) => AdminScreen(server: widget.session.server),
              ),
            ),
          ),
        ),
        const SizedBox(height: 16),
        Card(
          child: ListTile(
            leading: const Icon(Icons.route_outlined),
            title: const Text('内容平台 API'),
            subtitle: const Text('切换平台接口地址'),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute<void>(
                builder: (_) => PlatformEndpointsScreen(api: widget.api),
              ),
            ),
          ),
        ),
        const SizedBox(height: 16),
        ImageCacheCard(api: widget.api),
        const SizedBox(height: 16),
        AppUpdateCard(api: widget.api),
        const SizedBox(height: 8),
        const Text(
          '浏览过的图片会缓存到手机。下载正文仍保存到服务器，完整作品离线阅读将在后续版本提供。',
          textAlign: TextAlign.center,
        ),
      ],
    ),
  );
}
