import 'package:flutter/material.dart';

import 'api.dart';
import 'server_speed_test.dart';

class ServerSpeedScreen extends StatefulWidget {
  final LibraryApi api;
  final ServerSpeedTest? tester;
  const ServerSpeedScreen({super.key, required this.api, this.tester});

  @override
  State<ServerSpeedScreen> createState() => _ServerSpeedScreenState();
}

class _ServerSpeedScreenState extends State<ServerSpeedScreen>
    with WidgetsBindingObserver {
  late final ServerSpeedTest _test =
      widget.tester ?? ServerSpeedTest(api: widget.api);
  int _transferMiB = 8;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _test.addListener(_changed);
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.paused ||
        state == AppLifecycleState.hidden ||
        state == AppLifecycleState.detached) {
      _test.cancel();
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _test.removeListener(_changed);
    _test.dispose();
    super.dispose();
  }

  Widget _measurement(
    String title,
    IconData icon,
    SpeedMeasurement? result,
  ) => Card(
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            spacing: 12,
            runSpacing: 8,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              Icon(icon),
              Text(title),
              Text(
                result == null
                    ? '—'
                    : '${result.megabitsPerSecond.toStringAsFixed(2)} Mbps',
                style: Theme.of(context).textTheme.titleMedium,
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            result == null
                ? '未完成'
                : '${result.megabytesPerSecond.toStringAsFixed(2)} MB/s · '
                      '${(result.bytes / 1048576).toStringAsFixed(1)} MiB · '
                      '${(result.elapsed.inMilliseconds / 1000).toStringAsFixed(2)} 秒',
          ),
        ],
      ),
    ),
  );

  @override
  Widget build(BuildContext context) {
    final state = _test.snapshot;
    final status = switch (state.stage) {
      SpeedTestStage.idle => '准备就绪',
      SpeedTestStage.latency => '正在测量延迟 ${state.completedPings}/5',
      SpeedTestStage.download =>
        '正在下载 ${(state.receivedBytes / 1048576).toStringAsFixed(1)} / '
            '${state.transferBytes ~/ 1048576} MiB',
      SpeedTestStage.upload =>
        '正在上传 ${state.transferBytes ~/ 1048576} MiB，等待服务器确认',
      SpeedTestStage.complete => '测速完成',
      SpeedTestStage.cancelled => '已取消，已保留完成的结果',
      SpeedTestStage.failed => '测速未完成',
    };
    return Scaffold(
      appBar: AppBar(title: const Text('服务器测速')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.dns_outlined),
            title: const Text('当前服务器'),
            subtitle: SelectableText(widget.api.server.toString()),
          ),
          const Text('测量手机到此地址的连接；连接局域网地址时，结果为局域网速度。'),
          const SizedBox(height: 20),
          Text('测试流量（下载 + 上传）', style: Theme.of(context).textTheme.labelLarge),
          const SizedBox(height: 8),
          SegmentedButton<int>(
            segments: const [
              ButtonSegment(value: 8, label: Text('16 MiB')),
              ButtonSegment(value: 32, label: Text('64 MiB')),
            ],
            selected: {_transferMiB},
            onSelectionChanged: state.running
                ? null
                : (selected) => setState(() => _transferMiB = selected.single),
          ),
          const SizedBox(height: 8),
          const Text('下载、上传各测试一次。每项最多 20 秒；使用移动网络会消耗流量。'),
          const SizedBox(height: 20),
          Card(
            child: ListTile(
              leading: const Icon(Icons.network_ping),
              title: const Text('延迟'),
              subtitle: const Text('5 次请求的中位数'),
              trailing: Text(
                state.latencyMs == null
                    ? '—'
                    : '${state.latencyMs!.toStringAsFixed(1)} ms',
                style: Theme.of(context).textTheme.titleMedium,
              ),
            ),
          ),
          _measurement('下载', Icons.download_outlined, state.download),
          _measurement('上传', Icons.upload_outlined, state.upload),
          const SizedBox(height: 16),
          Text(status, key: const ValueKey('speed-status')),
          if (state.running) ...[
            const SizedBox(height: 12),
            LinearProgressIndicator(
              value: state.stage == SpeedTestStage.download
                  ? state.receivedBytes / state.transferBytes
                  : null,
            ),
          ],
          if (state.error != null) ...[
            const SizedBox(height: 12),
            Text(
              state.error!,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ],
          const SizedBox(height: 20),
          FilledButton.icon(
            onPressed: state.running
                ? _test.cancel
                : () => _test.start(transferBytes: _transferMiB * 1048576),
            icon: Icon(state.running ? Icons.stop : Icons.speed),
            label: Text(
              state.running
                  ? '取消测速'
                  : state.stage == SpeedTestStage.idle
                  ? '开始测速'
                  : '重新测速',
            ),
          ),
          const SizedBox(height: 12),
          const Text('速度包含请求耗时，供当前连接下的阅读、下载参考；测试数据不会存入书库。'),
        ],
      ),
    );
  }
}
