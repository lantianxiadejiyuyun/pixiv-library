import 'package:flutter/material.dart';

List<String> parseArticleParagraphs(String text) {
  final normalized = text.replaceAll('\r\n', '\n').replaceAll('\r', '\n');
  final blankLine = RegExp(r'\n[ \t\u3000]*\n+');
  return normalized
      .split(blankLine.hasMatch(normalized) ? blankLine : '\n')
      .where((line) => line.trim().isNotEmpty)
      .toList(growable: false);
}

/// Paragraph spacing is separate from line height; source text stays intact.
class ArticleBody extends StatefulWidget {
  final String text;
  final double fontSize;
  const ArticleBody({super.key, required this.text, required this.fontSize});

  @override
  State<ArticleBody> createState() => _ArticleBodyState();
}

class _ArticleBodyState extends State<ArticleBody> {
  late List<String> _paragraphs;

  @override
  void initState() {
    super.initState();
    _paragraphs = parseArticleParagraphs(widget.text);
  }

  @override
  void didUpdateWidget(ArticleBody oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.text != widget.text) {
      _paragraphs = parseArticleParagraphs(widget.text);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (int i = 0; i < _paragraphs.length; i++)
          Padding(
            padding: EdgeInsets.only(
              bottom: i == _paragraphs.length - 1
                  ? 0
                  : MediaQuery.textScalerOf(context).scale(widget.fontSize) *
                        .9,
            ),
            child: Text(
              _paragraphs[i],
              style: TextStyle(fontSize: widget.fontSize, height: 1.85),
            ),
          ),
      ],
    );
  }
}
