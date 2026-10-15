import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qiye_library/api.dart';
import 'package:qiye_library/binding_screen.dart';
import 'session_api_test.dart' show MemoryStore;

void main() {
  testWidgets(
    'Native binding screen fits a small phone and offers camera-free fallback',
    (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(
        MaterialApp(
          home: BindingScreen(
            pairing: PairingService(MemoryStore()),
            onBound: (_) {},
          ),
        ),
      );
      expect(find.text('扫码绑定服务器'), findsOneWidget);
      await tester.tap(find.text('粘贴绑定码'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField).last, 'invalid QR');
      await tester.tap(find.widgetWithText(FilledButton, '绑定'));
      await tester.pumpAndSettle();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.text('不是栖页绑定码'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );
}
