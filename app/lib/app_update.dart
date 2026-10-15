import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'api.dart';

class AndroidUpdate {
  final int code, size, minSdk;
  final String version, digest, notes, path, certificate;
  AndroidUpdate._(
    this.code,
    this.size,
    this.minSdk,
    this.version,
    this.digest,
    this.notes,
    this.path,
    this.certificate,
  );
  factory AndroidUpdate.fromJson(Map value) {
    final code = value['version_code'],
        size = value['size_bytes'],
        sdk = value['min_sdk'];
    final hash = value['sha256'], cert = value['certificate_sha256'];
    if (value['package_name'] != 'app.qiye.qiye_library' ||
        code is! int ||
        code <= 0 ||
        code > 2100000000 ||
        size is! int ||
        size <= 0 ||
        size > 256 * 1024 * 1024 ||
        sdk is! int ||
        sdk < 1 ||
        hash is! String ||
        !RegExp(r'^[a-f0-9]{64}$').hasMatch(hash) ||
        cert is! String ||
        !RegExp(r'^[a-f0-9]{64}$').hasMatch(cert) ||
        value['download_path'] != '/api/app-updates/android/$hash/apk' ||
        value['version_name'] is! String ||
        (value['version_name'] as String).length > 80 ||
        value['notes'] is! String ||
        (value['notes'] as String).length > 2000) {
      throw const ApiFailure('服务器更新信息无效');
    }
    return AndroidUpdate._(
      code,
      size,
      sdk,
      value['version_name'],
      hash,
      value['notes'],
      value['download_path'],
      cert,
    );
  }
}

class AppUpdateCard extends StatefulWidget {
  final LibraryApi api;
  const AppUpdateCard({super.key, required this.api});
  @override
  State<AppUpdateCard> createState() => _AppUpdateCardState();
}

class _AppUpdateCardState extends State<AppUpdateCard> {
  static const _native = MethodChannel('app.qiye/updater');
  Map? _installed;
  AndroidUpdate? _update;
  File? _file;
  bool _busy = false;
  int _received = 0;
  String? _notice;
  bool get _android =>
      !kIsWeb && defaultTargetPlatform == TargetPlatform.android;
  @override
  void initState() {
    super.initState();
    if (_android) _info();
  }

  Future<void> _info() async {
    try {
      final info = await _native.invokeMapMethod<String, dynamic>('info');
      if (mounted) setState(() => _installed = info);
    } catch (_) {
      if (mounted) setState(() => _notice = '无法读取版本信息，请重试');
    }
  }

  Future<void> _check() async {
    setState(() {
      _busy = true;
      _notice = null;
      _update = null;
      _file = null;
    });
    try {
      await _info();
      if (_installed == null) throw const ApiFailure('无法读取已安装版本');
      final response = await widget.api.get('/app-updates/android') as Map;
      if (!mounted) return;
      final raw = response['release'];
      if (raw == null) {
        setState(() => _notice = '服务器尚未发布更新');
        return;
      }
      final update = AndroidUpdate.fromJson(raw as Map);
      if (update.code <= (_installed!['version_code'] as num)) {
        setState(() => _notice = '已是最新版本');
        return;
      }
      if (update.minSdk > (_installed!['sdk'] as num)) {
        throw const ApiFailure('新版需要更高版本的 Android');
      }
      if (!(_installed!['certificates'] as List).contains(update.certificate)) {
        throw const ApiFailure('新版签名与已安装客户端不一致');
      }
      setState(() => _update = update);
    } catch (e) {
      if (mounted) {
        setState(() => _notice = e is ApiFailure ? e.message : '检查更新失败，请稍后重试');
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _download() async {
    setState(() {
      _busy = true;
      _received = 0;
      _notice = null;
    });
    try {
      final path = await _native.invokeMethod<String>('prepare');
      if (path == null) throw const ApiFailure('无法准备更新目录');
      if (!mounted) return;
      final file = File(path), update = _update!;
      await widget.api.downloadApk(
        update.path,
        file,
        size: update.size,
        digest: update.digest,
        isCancelled: () => !mounted,
        onProgress: (bytes) {
          if (mounted) setState(() => _received = bytes);
        },
      );
      if (mounted) {
        setState(() {
          _file = file;
          _notice = '下载及完整性校验完成，点击安装更新。';
        });
      }
    } catch (e) {
      if (mounted) {
        setState(
          () => _notice = e is ApiFailure ? e.message : '下载失败，请检查手机存储后重试',
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _install() async {
    setState(() {
      _busy = true;
      _notice = null;
    });
    try {
      final status = await _native.invokeMethod<String>('install', {
        'version_code': _update!.code,
        'sha256': _update!.digest,
      });
      if (mounted) {
        setState(
          () => _notice = status == 'permission'
              ? '请允许栖页安装应用，返回后再次点击“安装更新”。'
              : '请在系统安装界面确认。安装完成后重新打开 App。',
        );
      }
    } on PlatformException catch (e) {
      if (mounted) setState(() => _notice = e.message ?? '无法打开系统安装界面');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Card(
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('客户端更新', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 8),
          if (!_android) const Text('APK 在线更新仅适用于 Android。'),
          if (_installed != null)
            Text(
              '当前版本 ${_installed!['version_name']}（${_installed!['version_code']}）',
            ),
          if (_update != null) ...[
            Text(
              '可更新至 ${_update!.version} · ${(_update!.size / 1024 / 1024).toStringAsFixed(1)} MiB',
            ),
            if (_update!.notes.isNotEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 8),
                child: Text(_update!.notes),
              ),
            if (_busy && _received > 0) ...[
              LinearProgressIndicator(value: _received / _update!.size),
              Text(
                '下载 ${(_received * 100 / _update!.size).toStringAsFixed(0)}%',
              ),
            ],
            FilledButton.icon(
              onPressed: _busy
                  ? null
                  : _file == null
                  ? _download
                  : _install,
              icon: const Icon(Icons.system_update),
              label: Text(_file == null ? '下载更新' : '安装更新'),
            ),
          ],
          if (_android)
            OutlinedButton.icon(
              onPressed: _busy ? null : _check,
              icon: const Icon(Icons.refresh),
              label: const Text('检查更新'),
            ),
          if (_busy && _received == 0) const LinearProgressIndicator(),
          if (_notice != null) Text(_notice!),
        ],
      ),
    ),
  );
}
