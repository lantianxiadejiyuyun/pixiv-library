import 'package:flutter_secure_storage/flutter_secure_storage.dart';

class ReaderPreferences {
  final FlutterSecureStorage storage;
  const ReaderPreferences({this.storage = const FlutterSecureStorage()});
  Future<bool> flowMode() async =>
      await storage.read(key: 'qiye.comic.flow') == 'true';
  Future<void> saveFlowMode(bool flow) =>
      storage.write(key: 'qiye.comic.flow', value: '$flow');

  Future<double> fontSize() async {
    final value = double.tryParse(
      await storage.read(key: 'qiye.novel.font') ?? '',
    );
    return value != null && value.isFinite ? value.clamp(15, 30) : 19;
  }

  Future<void> saveFontSize(double size) =>
      storage.write(key: 'qiye.novel.font', value: '${size.clamp(15, 30)}');
}
