import 'package:flutter_secure_storage/flutter_secure_storage.dart';

class ReaderPreferences {
  final FlutterSecureStorage storage;
  const ReaderPreferences({this.storage = const FlutterSecureStorage()});
  Future<bool> flowMode() async =>
      await storage.read(key: 'qiye.comic.flow') == 'true';
  Future<void> saveFlowMode(bool flow) =>
      storage.write(key: 'qiye.comic.flow', value: '$flow');
}
