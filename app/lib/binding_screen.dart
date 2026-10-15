import 'dart:io';
import 'package:flutter/material.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'api.dart';
import 'session.dart';

class BindingScreen extends StatefulWidget {
  final PairingService pairing;
  final ValueChanged<DeviceSession> onBound;
  final String? notice;
  const BindingScreen({
    super.key,
    required this.pairing,
    required this.onBound,
    this.notice,
  });
  @override
  State<BindingScreen> createState() => _BindingScreenState();
}

class _BindingScreenState extends State<BindingScreen> {
  final _name = TextEditingController(
    text: Platform.isIOS ? '我的 iPhone' : '我的 Android',
  );
  bool _busy = false;
  String? _error, _connecting;
  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  Future<void> _bind(String? raw) async {
    if (raw == null || !mounted || _busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final code = PairingCode.parse(raw);
      final name = _name.text.trim();
      if (name.isEmpty ||
          name.length > 100 ||
          RegExp(r'[\x00-\x1f\x7f]').hasMatch(name)) {
        throw const FormatException('请填写 1–100 字的设备名称');
      }
      setState(() {
        _connecting = code.server.toString();
      });
      final session = await widget.pairing.bind(code, name);
      if (mounted) widget.onBound(session);
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = e is FormatException
              ? e.message.toString()
              : e is ApiFailure
              ? e.message
              : '绑定失败，请重新生成二维码后重试';
        });
      }
    } finally {
      if (mounted) {
        setState(() {
          _busy = false;
          _connecting = null;
        });
      }
    }
  }

  Future<void> _manual() async {
    final controller = TextEditingController();
    final raw = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('粘贴绑定码'),
        content: TextField(
          controller: controller,
          minLines: 3,
          maxLines: 5,
          maxLength: 4096,
          autocorrect: false,
          enableSuggestions: false,
          decoration: const InputDecoration(
            hintText: '在网页点击“复制绑定码”后粘贴到这里',
            border: OutlineInputBorder(),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, controller.text),
            child: const Text('绑定'),
          ),
        ],
      ),
    );
    // Dialog's animation may retain its TextField for a frame.
    await Future<void>.delayed(const Duration(milliseconds: 250));
    controller.clear();
    controller.dispose();
    await _bind(raw);
  }

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 500),
            child: ListView(
              padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 48),
              shrinkWrap: true,
              children: [
                Align(
                  alignment: Alignment.centerLeft,
                  child: Container(
                    padding: const EdgeInsets.all(18),
                    decoration: BoxDecoration(
                      color: colors.primaryContainer,
                      borderRadius: BorderRadius.circular(24),
                    ),
                    child: Icon(
                      Icons.auto_stories_rounded,
                      size: 46,
                      color: colors.onPrimaryContainer,
                    ),
                  ),
                ),
                const SizedBox(height: 28),
                Text(
                  '栖页',
                  style: Theme.of(context).textTheme.displaySmall?.copyWith(
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  '连接你的书库',
                  style: Theme.of(context).textTheme.headlineSmall,
                ),
                const SizedBox(height: 14),
                Text(
                  '在电脑或 NAS 网页打开「设置 → 手机 App」，生成二维码后扫描。',
                  style: Theme.of(
                    context,
                  ).textTheme.bodyLarge?.copyWith(height: 1.6),
                ),
                const SizedBox(height: 28),
                TextField(
                  controller: _name,
                  enabled: !_busy,
                  maxLength: 100,
                  decoration: const InputDecoration(
                    labelText: '设备名称',
                    prefixIcon: Icon(Icons.smartphone_rounded),
                    border: OutlineInputBorder(),
                    counterText: '',
                  ),
                ),
                const SizedBox(height: 20),
                FilledButton.icon(
                  onPressed: _busy
                      ? null
                      : () async {
                          final raw = await Navigator.of(context).push<String>(
                            MaterialPageRoute(
                              builder: (_) => const ScannerScreen(),
                            ),
                          );
                          await _bind(raw);
                        },
                  icon: const Icon(Icons.qr_code_scanner_rounded),
                  label: const Padding(
                    padding: EdgeInsets.symmetric(vertical: 14),
                    child: Text('扫码绑定服务器'),
                  ),
                ),
                const SizedBox(height: 8),
                TextButton.icon(
                  onPressed: _busy ? null : _manual,
                  icon: const Icon(Icons.content_paste_rounded),
                  label: const Text('粘贴绑定码'),
                ),
                if (_busy) ...[
                  const SizedBox(height: 16),
                  const LinearProgressIndicator(),
                  const SizedBox(height: 12),
                  Text('正在连接 ${_connecting ?? '服务器'}…'),
                ],
                if (_error != null) ...[
                  const SizedBox(height: 16),
                  Text(_error!, style: TextStyle(color: colors.error)),
                ],
                if (widget.notice != null) ...[
                  const SizedBox(height: 16),
                  Text(
                    widget.notice!,
                    style: TextStyle(color: colors.onSurfaceVariant),
                  ),
                ],
                const SizedBox(height: 32),
                Text(
                  '手机和服务器需互相可达。局域网请填写电脑或 NAS 的 IP，不能填写 localhost 或 127.0.0.1。',
                  style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    color: colors.onSurfaceVariant,
                    height: 1.5,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class ScannerScreen extends StatefulWidget {
  const ScannerScreen({super.key});
  @override
  State<ScannerScreen> createState() => _ScannerScreenState();
}

class _ScannerScreenState extends State<ScannerScreen> {
  final _controller = MobileScannerController(
    formats: const [BarcodeFormat.qrCode],
    detectionSpeed: DetectionSpeed.noDuplicates,
  );
  bool _done = false;
  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('扫描服务器二维码')),
    body: Stack(
      fit: StackFit.expand,
      children: [
        MobileScanner(
          controller: _controller,
          useAppLifecycleState: true,
          onDetectError: (_, _) {},
          onDetect: (capture) {
            if (_done) return;
            for (final code in capture.barcodes) {
              if (code.rawValue == null) continue;
              _done = true;
              Navigator.pop(context, code.rawValue);
              break;
            }
          },
          errorBuilder: (context, error) => Center(
            child: Padding(
              padding: const EdgeInsets.all(28),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.no_photography_outlined, size: 48),
                  const SizedBox(height: 18),
                  const Text(
                    '相机暂不可用。请在系统设置中允许相机权限，或返回粘贴绑定码。',
                    textAlign: TextAlign.center,
                  ),
                  const SizedBox(height: 16),
                  FilledButton(
                    onPressed: () => Navigator.pop(context),
                    child: const Text('返回'),
                  ),
                ],
              ),
            ),
          ),
        ),
        IgnorePointer(
          child: Center(
            child: Container(
              width: 260,
              height: 260,
              decoration: BoxDecoration(
                border: Border.all(color: Colors.white70, width: 2),
                borderRadius: BorderRadius.circular(28),
              ),
            ),
          ),
        ),
        const Positioned(
          left: 24,
          right: 24,
          bottom: 42,
          child: IgnorePointer(
            child: Card(
              color: Color(0xcc202128),
              child: Padding(
                padding: EdgeInsets.all(16),
                child: Text(
                  '将网页上的绑定二维码放入框内',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: Colors.white),
                ),
              ),
            ),
          ),
        ),
      ],
    ),
  );
}
