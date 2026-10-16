import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:path_provider/path_provider.dart';

class ImageCacheStats {
  final int bytes;
  final int entries;
  final int maxBytes;
  final int memoryBytes;
  const ImageCacheStats({
    required this.bytes,
    required this.entries,
    required this.maxBytes,
    required this.memoryBytes,
  });
}

class _MemoryImage {
  final Uint8List bytes;
  final DateTime written;
  _MemoryImage(this.bytes, this.written);
}

class _DiskImage {
  final File file;
  final int size;
  DateTime accessed;
  _DiskImage(this.file, this.size, this.accessed);
}

/// Cache of compressed image bytes. The operating system may discard disk data.
/// No credentials, URLs, or image metadata are written to filenames or records.
class AppImageCache {
  static final shared = AppImageCache();
  static const _magic = 0x51495945494d4731; // QIYEIMG1
  static const _headerSize = 16;
  final Future<Directory> Function() directoryProvider;
  final DateTime Function() clock;
  final int maxMemoryBytes;
  final int maxDiskBytes;
  final int maxPendingWriteBytes;
  final _memory = <String, _MemoryImage>{};
  final _disk = <String, _DiskImage>{};
  final _pending = <String, Future<Uint8List>>{};
  final _pendingDiskKeys = <String>{};
  final _generations = <String, int>{};
  final _keyGenerations = <String, int>{};
  int _memoryBytes = 0;
  int _pendingWriteBytes = 0;
  Future<void> _diskTail = Future.value();
  Directory? _root;

  AppImageCache({
    Future<Directory> Function()? directoryProvider,
    DateTime Function()? clock,
    this.maxMemoryBytes = 32 * 1024 * 1024,
    this.maxDiskBytes = 16 * 1024 * 1024 * 1024,
    this.maxPendingWriteBytes = 32 * 1024 * 1024,
  }) : directoryProvider = directoryProvider ?? getApplicationCacheDirectory,
       clock = clock ?? DateTime.now;

  static String namespace(Uri server, String? token) => sha256
      .convert(utf8.encode(jsonEncode([server.origin, token])))
      .toString();

  String _key(String namespace, Uri uri) =>
      '$namespace/${sha256.convert(utf8.encode(uri.toString()))}';

  Future<T> _serial<T>(Future<T> Function() operation) {
    final result = _diskTail.then((_) => operation());
    _diskTail = result.then<void>((_) {}, onError: (Object _, StackTrace _) {});
    return result;
  }

  Future<Directory> _directory() async {
    if (_root != null) return _root!;
    final base = await directoryProvider();
    final root = Directory.fromUri(base.uri.resolve('qiye-images-v1/'));
    await root.create(recursive: true);
    // Scan once, then keep a small index; reads never rescan the library.
    await for (final namespace in root.list(followLinks: false)) {
      if (namespace is! Directory ||
          !RegExp(r'^[a-f0-9]{64}$').hasMatch(
            namespace.uri.pathSegments.where((part) => part.isNotEmpty).last,
          )) {
        continue;
      }
      final prefix = namespace.uri.pathSegments.where((p) => p.isNotEmpty).last;
      await for (final entry in namespace.list(followLinks: false)) {
        if (entry is! File) continue;
        final name = entry.uri.pathSegments.last;
        if (RegExp(r'^[a-f0-9]{64}\.img\.tmp$').hasMatch(name)) {
          await entry.delete();
          continue;
        }
        if (!RegExp(r'^[a-f0-9]{64}\.img$').hasMatch(name)) continue;
        final stat = await entry.stat();
        _disk['$prefix/${name.substring(0, 64)}'] = _DiskImage(
          entry,
          stat.size,
          stat.modified,
        );
      }
    }
    _root = root;
    await _trimDisk();
    return root;
  }

  void _forgetMemory(String key) {
    final old = _memory.remove(key);
    if (old != null) _memoryBytes -= old.bytes.length;
  }

  void _remember(String key, Uint8List bytes, DateTime written) {
    _forgetMemory(key);
    if (bytes.length > maxMemoryBytes) return;
    _memory[key] = _MemoryImage(bytes, written);
    _memoryBytes += bytes.length;
    while (_memoryBytes > maxMemoryBytes && _memory.isNotEmpty) {
      _forgetMemory(_memory.keys.first);
    }
  }

  Future<void> _deleteDisk(String key) async {
    final entry = _disk.remove(key);
    if (entry != null && await entry.file.exists()) await entry.file.delete();
  }

  Future<void> _trimDisk() async {
    var size = _disk.values.fold<int>(0, (sum, entry) => sum + entry.size);
    if (size <= maxDiskBytes) return;
    final oldest = _disk.keys.toList()
      ..sort((a, b) => _disk[a]!.accessed.compareTo(_disk[b]!.accessed));
    for (final key in oldest) {
      size -= _disk[key]!.size;
      await _deleteDisk(key);
      if (size <= maxDiskBytes) break;
    }
  }

  bool _expired(DateTime written, Duration ttl) {
    final age = clock().difference(written);
    return age.isNegative || age >= ttl;
  }

  Future<Uint8List> get({
    required String namespace,
    required Uri uri,
    required Duration ttl,
    required Future<Uint8List> Function() load,
    bool Function()? mayStore,
  }) {
    final key = _key(namespace, uri);
    final memory = _memory.remove(key);
    if (memory != null) {
      if (!_expired(memory.written, ttl)) {
        _memory[key] = memory;
        final accessed = clock();
        // Touch the disk LRU asynchronously without delaying a memory hit.
        unawaited(
          _serial(() async {
            final entry = _disk[key];
            if (entry == null) return;
            entry.accessed = accessed;
            try {
              await entry.file.setLastModified(entry.accessed);
            } catch (_) {
              /* Cache storage is optional. */
            }
          }),
        );
        return Future.value(memory.bytes);
      }
      _memoryBytes -= memory.bytes.length;
    }
    final pending = _pending[key];
    if (pending != null) return pending;
    final generation = _generations[namespace] ?? 0;
    final keyGeneration = _keyGenerations[key] ?? 0;
    bool current() =>
        generation == (_generations[namespace] ?? 0) &&
        keyGeneration == (_keyGenerations[key] ?? 0) &&
        (mayStore?.call() ?? true);
    late final Future<Uint8List> future;
    future =
        (() async {
          // Once indexed, an unrelated pending write must not delay a known
          // miss. A pending write for this exact key is still awaited/reused.
          final diskBytes =
              _root != null &&
                  !_disk.containsKey(key) &&
                  !_pendingDiskKeys.contains(key)
              ? null
              : await _serial(() async {
                  try {
                    await _directory();
                    if (!current()) return null;
                    final entry = _disk[key];
                    if (entry == null) return null;
                    final record = await entry.file.readAsBytes();
                    if (record.length <= _headerSize ||
                        ByteData.sublistView(record).getInt64(0) != _magic) {
                      await _deleteDisk(key);
                      return null;
                    }
                    final written = DateTime.fromMillisecondsSinceEpoch(
                      ByteData.sublistView(record).getInt64(8),
                      isUtc: true,
                    );
                    if (_expired(written, ttl)) {
                      await _deleteDisk(key);
                      return null;
                    }
                    final bytes = Uint8List.sublistView(record, _headerSize);
                    entry.accessed = clock();
                    await entry.file.setLastModified(entry.accessed);
                    if (!current()) return null;
                    _remember(key, bytes, written);
                    return bytes;
                  } catch (_) {
                    // Missing, full, or unavailable storage must not prevent reading.
                    return null;
                  }
                });
          if (diskBytes != null) return diskBytes;
          final bytes = await load();
          if (bytes.isEmpty || !current()) return bytes;
          final written = clock();
          _remember(key, bytes, written);
          if (bytes.length + _headerSize > maxDiskBytes ||
              _pendingWriteBytes + bytes.length > maxPendingWriteBytes) {
            return bytes;
          }
          _pendingWriteBytes += bytes.length;
          _pendingDiskKeys.add(key);
          // First paint must not wait for slow flash storage; stats/clear serialize
          // behind this write when their caller needs the completed disk state.
          unawaited(
            _serial(() async {
              File? temporary;
              try {
                if (!current()) return;
                final root = await _directory();
                if (!current()) return;
                final folder = Directory.fromUri(
                  root.uri.resolve('$namespace/'),
                );
                await folder.create(recursive: true);
                final file = File.fromUri(root.uri.resolve('$key.img'));
                temporary = File('${file.path}.tmp');
                final header = ByteData(_headerSize)
                  ..setInt64(0, _magic)
                  ..setInt64(8, written.toUtc().millisecondsSinceEpoch);
                final sink = temporary.openWrite();
                try {
                  sink.add(header.buffer.asUint8List());
                  sink.add(bytes);
                  await sink.flush();
                } finally {
                  await sink.close();
                }
                if (!current()) {
                  await temporary.delete();
                  return;
                }
                if (await file.exists()) await file.delete();
                await temporary.rename(file.path);
                await file.setLastModified(written);
                if (!current()) {
                  await file.delete();
                  return;
                }
                _disk[key] = _DiskImage(
                  file,
                  bytes.length + _headerSize,
                  written,
                );
                await _trimDisk();
              } catch (_) {
                /* Disk caching is best effort. */
              } finally {
                _pendingWriteBytes -= bytes.length;
                _pendingDiskKeys.remove(key);
                try {
                  if (temporary != null && await temporary.exists()) {
                    await temporary.delete();
                  }
                } catch (_) {
                  /* Startup removes any abandoned temporary record. */
                }
              }
            }),
          );
          return bytes;
        })().whenComplete(() {
          if (identical(_pending[key], future)) _pending.remove(key);
        });
    _pending[key] = future;
    return future;
  }

  Future<ImageCacheStats> stats(String namespace) => _serial(() async {
    try {
      await _directory();
    } catch (_) {
      /* Memory-only operation is supported. */
    }
    final entries = _disk.entries.where((e) => e.key.startsWith('$namespace/'));
    return ImageCacheStats(
      bytes: entries.fold(0, (sum, entry) => sum + entry.value.size),
      entries: entries.length,
      maxBytes: maxDiskBytes,
      memoryBytes: _memory.entries
          .where((e) => e.key.startsWith('$namespace/'))
          .fold(0, (sum, entry) => sum + entry.value.bytes.length),
    );
  });

  Future<void> evict(String namespace, Uri uri) {
    final key = _key(namespace, uri);
    _keyGenerations[key] = (_keyGenerations[key] ?? 0) + 1;
    _pending.remove(key);
    _forgetMemory(key);
    return _serial(() async {
      try {
        await _directory();
        await _deleteDisk(key);
      } catch (_) {
        /* Retry can always continue over the network. */
      }
    });
  }

  /// Invalidates pending work before awaiting disk IO. A late request may finish
  /// for its caller, but cannot refill memory or disk after clear/unauthorize.
  void release(String namespace) {
    _generations[namespace] = (_generations[namespace] ?? 0) + 1;
    _pending.removeWhere((key, _) => key.startsWith('$namespace/'));
    for (final key
        in _memory.keys.where((k) => k.startsWith('$namespace/')).toList()) {
      _forgetMemory(key);
    }
  }

  Future<void> clear(String namespace) {
    release(namespace);
    return _serial(() async {
      final root = await _directory();
      final folder = Directory.fromUri(root.uri.resolve('$namespace/'));
      if (await folder.exists()) await folder.delete(recursive: true);
      _disk.removeWhere((key, _) => key.startsWith('$namespace/'));
    });
  }
}
