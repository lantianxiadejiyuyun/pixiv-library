import 'package:flutter/material.dart';
import 'api.dart';
import 'models.dart';

Future<int?> editScore(
  BuildContext context,
  LibraryApi api,
  LibraryItem item, {
  ValueChanged<int>? onSaved,
}) => showModalBottomSheet<int>(
  context: context,
  isScrollControlled: true,
  showDragHandle: true,
  builder: (_) => ScoreEditor(api: api, item: item, onSaved: onSaved),
);

class ScoreEditor extends StatefulWidget {
  final LibraryApi api;
  final LibraryItem item;
  final ValueChanged<int>? onSaved;
  const ScoreEditor({
    super.key,
    required this.api,
    required this.item,
    this.onSaved,
  });

  @override
  State<ScoreEditor> createState() => _ScoreEditorState();
}

class _ScoreEditorState extends State<ScoreEditor> {
  late int _score;
  bool _busy = false;
  String? _message;

  @override
  void initState() {
    super.initState();
    _score = widget.item.userScore;
  }

  Future<void> _save() async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _message = null;
    });
    try {
      final result =
          await widget.api.put('/items/${widget.item.id}/score', {
                'score': _score,
              })
              as Map;
      final score = (result['user_score'] as num).toInt();
      widget.onSaved?.call(score);
      if (mounted) Navigator.pop(context, score);
    } catch (e) {
      if (mounted) {
        setState(() {
          _busy = false;
          _message = e is ApiFailure ? e.message : '评分保存失败，请重试';
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) => SafeArea(
    top: false,
    child: SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(24, 0, 24, 24),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('我的评分', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 8),
          Text(
            widget.item.displayTitle,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
          ),
          const SizedBox(height: 16),
          Wrap(
            alignment: WrapAlignment.center,
            children: [
              for (int i = 1; i <= 5; i++)
                IconButton(
                  tooltip: '$i 星',
                  isSelected: i <= _score,
                  onPressed: _busy ? null : () => setState(() => _score = i),
                  icon: Icon(
                    i <= _score
                        ? Icons.star_rounded
                        : Icons.star_outline_rounded,
                    color: Theme.of(context).colorScheme.primary,
                  ),
                ),
            ],
          ),
          Text(
            _score == 0 ? '未评分' : '$_score / 5',
            textAlign: TextAlign.center,
          ),
          TextButton(
            onPressed: _busy || _score == 0
                ? null
                : () => setState(() => _score = 0),
            child: const Text('清除评分'),
          ),
          if (_message != null) ...[
            Text(
              _message!,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
            const SizedBox(height: 12),
          ],
          FilledButton(
            onPressed: _busy ? null : _save,
            child: Text(_busy ? '保存中…' : '保存评分'),
          ),
        ],
      ),
    ),
  );
}
