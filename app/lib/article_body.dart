import 'package:flutter/material.dart';

/// Paragraph spacing is separate from line height; source text stays intact.
class ArticleBody extends StatelessWidget {
  final String text;
  final double fontSize;
  const ArticleBody({super.key, required this.text, required this.fontSize});

  @override
  Widget build(BuildContext context) {
    final normalized = text.replaceAll('\r\n', '\n').replaceAll('\r', '\n');
    final blankLine = RegExp(r'\n[ \t\u3000]*\n+');
    final paragraphs = normalized
        .split(blankLine.hasMatch(normalized) ? blankLine : '\n')
        .where((line) => line.trim().isNotEmpty)
        .toList();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (int i = 0; i < paragraphs.length; i++)
          Padding(
            padding: EdgeInsets.only(
              bottom: i == paragraphs.length - 1
                  ? 0
                  : MediaQuery.textScalerOf(context).scale(fontSize) * .9,
            ),
            child: Text(
              paragraphs[i],
              style: TextStyle(fontSize: fontSize, height: 1.85),
            ),
          ),
      ],
    );
  }
}
