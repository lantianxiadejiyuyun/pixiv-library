import 'dart:async';
import 'package:flutter/material.dart';
import 'api.dart';

Map<String, dynamic> _map(Object? value) =>
    value is Map ? Map<String, dynamic>.from(value) : {};
List<Map<String, dynamic>> _rows(Object? value) =>
    value is List ? value.map(_map).toList(growable: false) : [];
num? _number(Object? value) => value is num && value.isFinite ? value : null;
String _percent(Object? value) =>
    _number(value) == null ? '采样中' : '${(_number(value)!).toStringAsFixed(1)}%';
String _size(Object? value) {
  final raw = _number(value);
  if (raw == null) return '—';
  double number = raw.toDouble();
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  var unit = 0;
  while (number >= 1024 && unit < units.length - 1) {
    number /= 1024;
    unit++;
  }
  return '${number.toStringAsFixed(unit == 0 ? 0 : 1)} ${units[unit]}';
}

const _roles = {
  'api': 'Web / API',
  'download': '自动下载',
  'manual_download': '手动下载',
  'details': '详情解析',
  'checks': '作者与订阅检查',
  'retry': '失败重试',
  'ai': 'AI 识别',
  'translation': 'AI 汉化',
  'other': '其他任务',
  'other_process': '其他服务进程',
};
const _states = {
  'running': '运行中',
  'starting': '启动中',
  'restarting': '重启中',
  'unresponsive': '无响应',
  'unavailable': '暂不可用',
};

class ServerPerformanceScreen extends StatefulWidget {
  final LibraryApi api;
  const ServerPerformanceScreen({super.key, required this.api});
  @override
  State<ServerPerformanceScreen> createState() =>
      _ServerPerformanceScreenState();
}

class _ServerPerformanceScreenState extends State<ServerPerformanceScreen>
    with WidgetsBindingObserver {
  Map<String, dynamic>? _data;
  String? _error;
  bool _loading = false, _paused = false, _foreground = true;
  Timer? _timer;
  int _revision = 0, _failures = 0;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _load();
  }

  @override
  void dispose() {
    _revision++;
    _timer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    _revision++;
    _timer?.cancel();
    _loading = false;
    if (_foreground && !_paused) _load();
  }

  Future<void> _load() async {
    if (!mounted || _loading || _paused || !_foreground) return;
    final revision = ++_revision;
    _timer?.cancel();
    setState(() => _loading = true);
    try {
      final value = _map(await widget.api.get('/server/performance'));
      if (!mounted || revision != _revision) return;
      setState(() {
        _data = value;
        _error = null;
        _failures = 0;
      });
    } catch (e) {
      if (mounted && revision == _revision) {
        setState(() {
          _error = e is ApiFailure ? e.message : '无法读取服务器性能';
          _failures++;
        });
      }
    } finally {
      if (mounted && revision == _revision) {
        setState(() => _loading = false);
        if (!_paused && _foreground) {
          _timer = Timer(Duration(seconds: _failures > 2 ? 10 : 2), _load);
        }
      }
    }
  }

  void _toggle() {
    _revision++;
    _timer?.cancel();
    setState(() {
      _paused = !_paused;
      _loading = false;
    });
    if (!_paused) _load();
  }

  Widget _bar(Object? value) => LinearProgressIndicator(
    value: _number(value) == null ? 0 : (_number(value)! / 100).clamp(0.0, 1.0),
    minHeight: 5,
    borderRadius: BorderRadius.circular(8),
  );
  Widget _metric(String title, Object? value, String description) => Card(
    margin: EdgeInsets.zero,
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(title, style: Theme.of(context).textTheme.labelLarge),
          const SizedBox(height: 14),
          FittedBox(
            fit: BoxFit.scaleDown,
            child: Text(
              _percent(value),
              style: Theme.of(
                context,
              ).textTheme.headlineMedium?.copyWith(fontWeight: FontWeight.w600),
            ),
          ),
          const SizedBox(height: 14),
          _bar(value),
          const SizedBox(height: 10),
          Text(description, style: Theme.of(context).textTheme.bodySmall),
        ],
      ),
    ),
  );
  Widget _section(String title, List<Widget> children) => Card(
    child: Padding(
      padding: const EdgeInsets.all(18),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(title, style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 16),
          ...children,
        ],
      ),
    ),
  );

  @override
  Widget build(BuildContext context) {
    final current = _map(_data?['current']),
        cpu = _map(current['cpu']),
        memory = _map(current['memory']);
    final host = _map(memory['host']),
        service = _map(memory['service']),
        network = _map(current['network']),
        database = _map(_data?['database']);
    final queues = {
      for (final row in _rows(database['queues'])) row['id']: row,
    };
    final container = current['container'] == true;
    final history = _rows(_data?['history']);
    return Scaffold(
      appBar: AppBar(
        title: const Text('服务器性能'),
        actions: [
          IconButton(
            tooltip: _paused ? '恢复刷新' : '暂停刷新',
            onPressed: _toggle,
            icon: Icon(_paused ? Icons.play_arrow : Icons.pause),
          ),
          IconButton(
            tooltip: '刷新性能',
            onPressed: _loading || _paused ? null : _load,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Text(
              _paused
                  ? '已暂停刷新'
                  : _error != null
                  ? '连接中断 · 保留上次采样'
                  : _data?['status'] == 'ready'
                  ? '实时采样 · 每 2 秒刷新'
                  : '等待有效采样',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            if (_loading && _data == null)
              const Padding(
                padding: EdgeInsets.all(24),
                child: Center(child: CircularProgressIndicator()),
              ),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 12),
                child: Text(
                  _error!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              ),
            if (current.isNotEmpty) ...[
              const SizedBox(height: 12),
              Text(
                '${current['platform']} · ${container ? 'Docker 容器' : '本机进程'}',
                style: Theme.of(context).textTheme.bodySmall,
              ),
              const SizedBox(height: 16),
              LayoutBuilder(
                builder: (context, constraints) => Wrap(
                  spacing: 12,
                  runSpacing: 12,
                  children: [
                    SizedBox(
                      width: (constraints.maxWidth - 12) / 2,
                      child: _metric(
                        '服务器 CPU',
                        cpu['host_percent'],
                        '${cpu['logical_cpus']} 个逻辑 CPU',
                      ),
                    ),
                    SizedBox(
                      width: (constraints.maxWidth - 12) / 2,
                      child: _metric(
                        '书库服务 CPU',
                        cpu['service_percent'],
                        '按 ${cpu['service_capacity']} 核容量计算',
                      ),
                    ),
                    SizedBox(
                      width: (constraints.maxWidth - 12) / 2,
                      child: _metric(
                        '服务器内存',
                        host['percent'],
                        '${_size(host['used_bytes'])} / ${_size(host['total_bytes'])}',
                      ),
                    ),
                    SizedBox(
                      width: (constraints.maxWidth - 12) / 2,
                      child: _metric(
                        '书库服务内存',
                        service['percent'],
                        '${_size(service['used_bytes'])} / ${_size(service['limit_bytes'])} · ${container ? '工作集' : 'RSS 合计'}',
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),
              for (final metric in ['cpu', 'memory'])
                _section(
                  metric == 'cpu' ? 'CPU 趋势 · 最近 5 分钟' : '内存趋势 · 最近 5 分钟',
                  [
                    Semantics(
                      label: '${metric == 'cpu' ? 'CPU' : '内存'}使用率最近五分钟趋势',
                      child: SizedBox(
                        height: 130,
                        width: double.infinity,
                        child: CustomPaint(
                          painter: _TrendPainter(
                            history,
                            'host_$metric',
                            'service_$metric',
                            Theme.of(context).colorScheme.primary,
                            Theme.of(context).colorScheme.outlineVariant,
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(height: 10),
                    const Text('灰色：服务器整体　主题色：书库服务'),
                  ],
                ),
              _section(network['scope'] == 'container' ? '容器网络' : '服务器网络', [
                Text(
                  '↓ 接收 ${_size(network['download_bps'])}/s　↑ 发送 ${_size(network['upload_bps'])}/s',
                ),
                const SizedBox(height: 10),
                Text(
                  container ? '宿主机 NAS 挂载传输不计入容器网络。' : '不包含回环接口，多个活动接口流量合计。',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ]),
              _section('数据库与任务', [
                Text(
                  database['status'] == 'ok' && database['stale'] != true
                      ? '数据库响应 ${database['latency_ms']} ms'
                      : '数据库暂不可用或采样已过期',
                ),
                const SizedBox(height: 10),
                Text(
                  queues.isEmpty
                      ? '队列采样中'
                      : '等待 ${queues.values.fold<int>(0, (n, q) => n + ((_number(q['queued']) ?? 0).toInt()))}　执行中 ${queues.values.fold<int>(0, (n, q) => n + ((_number(q['running']) ?? 0).toInt()))}',
                ),
                const SizedBox(height: 10),
                const Text('队列每 10 秒采样，不扫描历史任务详情。'),
              ]),
              for (final disk in _rows(_data?['storage']))
                _section(
                  '${disk['label']}${_data?['storage_stale'] == true ? ' · 数据已过期' : ''}',
                  [
                    if (disk['status'] == 'ok') ...[
                      Text(
                        '可用 ${_size(disk['free_bytes'])} / 共 ${_size(disk['total_bytes'])}',
                      ),
                      const SizedBox(height: 12),
                      _bar(disk['percent']),
                      const SizedBox(height: 10),
                      Text('已使用 ${_percent(disk['percent'])} · 每 30 秒采样'),
                    ] else
                      const Text('存储暂不可用，其他指标继续采样。'),
                  ],
                ),
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 12),
                child: Text('服务进程与队列'),
              ),
              for (final process in _rows(current['processes']))
                _section(_roles[process['lane']] ?? '服务进程', [
                  Text(
                    '${_states[process['state']] ?? process['state']} · PID ${process['pid'] ?? '—'}${process['restarts'] != 0 ? ' · 已重启 ${process['restarts']} 次' : ''}',
                  ),
                  const SizedBox(height: 10),
                  Text(
                    'CPU ${_percent(process['cpu_percent'])}　内存 ${_size(process['rss_bytes'])}',
                  ),
                  const SizedBox(height: 8),
                  Text(
                    '绑定 CPU：${(process['cpus'] as List?)?.isNotEmpty == true ? (process['cpus'] as List).join(', ') : '未绑定'}',
                  ),
                  if (queues[process['lane']] case final queue?) ...[
                    const SizedBox(height: 8),
                    Text(
                      '任务 ${queue['running']} / ${queue['concurrency'] ?? '—'}　等待 ${queue['queued']}${queue['effective_paused'] == true || queue['paused'] == true ? ' · 队列已暂停' : ''}',
                    ),
                  ],
                ]),
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 16),
                child: Text(
                  '单进程 CPU 以一个逻辑核为 100%。${container ? '服务资源不含独立的 MySQL 容器；服务器整体指标包含虚拟机其他服务。' : 'RSS 合计可能重复统计共享页。'}',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _TrendPainter extends CustomPainter {
  final List<Map<String, dynamic>> points;
  final String first, second;
  final Color color, gridColor;
  _TrendPainter(
    this.points,
    this.first,
    this.second,
    this.color,
    this.gridColor,
  );
  @override
  void paint(Canvas canvas, Size size) {
    final grid = Paint()
      ..color = gridColor
      ..strokeWidth = .5;
    for (final ratio in [0.0, .5, 1.0]) {
      canvas.drawLine(
        Offset(0, size.height * ratio),
        Offset(size.width, size.height * ratio),
        grid,
      );
    }
    if (points.isEmpty) return;
    final end = _number(points.last['at']);
    if (end == null) return;
    for (final key in [first, second]) {
      final path = Path();
      var connected = false;
      for (final point in points) {
        final value = _number(point[key]), at = _number(point['at']);
        if (value == null || at == null) {
          connected = false;
          continue;
        }
        final x = ((at - end + 300) / 300).clamp(0.0, 1.0) * size.width;
        final y = (1 - (value / 100).clamp(0.0, 1.0)) * size.height;
        if (connected) {
          path.lineTo(x, y);
        } else {
          path.moveTo(x, y);
          connected = true;
        }
      }
      canvas.drawPath(
        path,
        Paint()
          ..color = (key == first ? Colors.blueGrey : color)
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2
          ..strokeJoin = StrokeJoin.round,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _TrendPainter old) =>
      old.points != points || old.color != color || old.gridColor != gridColor;
}
