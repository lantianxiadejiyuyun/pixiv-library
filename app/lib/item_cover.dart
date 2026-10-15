import 'package:flutter/material.dart';
import 'api.dart';
import 'api_image.dart';
import 'models.dart';

class ItemCover extends StatelessWidget {
  final LibraryApi api;
  final LibraryItem item;
  const ItemCover({super.key, required this.api, required this.item});
  @override
  Widget build(BuildContext context) => Stack(
    fit: StackFit.expand,
    children: [
      if (item.coverUrl != null)
        ApiImage(api: api, path: item.coverUrl!)
      else
        ColoredBox(
          color: Theme.of(context).colorScheme.surfaceContainerHighest,
          child: Icon(
            item.isText
                ? Icons.auto_stories_outlined
                : Icons.menu_book_outlined,
            size: 40,
          ),
        ),
      if (item.isFavorite)
        const Positioned(
          right: 8,
          top: 8,
          child: Icon(Icons.favorite, color: Colors.pinkAccent, size: 22),
        ),
      Positioned(
        left: 7,
        right: 7,
        bottom: 7,
        child: Wrap(
          spacing: 5,
          runSpacing: 4,
          children: [
            if (!item.isText && item.pageCount > 0)
              _badge('${item.pageCount} P', '作品页数'),
            if (item.sourceBookmarkCount != null)
              _badge('★ ${item.sourceBookmarkCount}', '平台收藏人数（同步时）'),
            if (item.userScore > 0) _badge('评分 ${item.userScore}/5', '我的评分'),
          ],
        ),
      ),
    ],
  );
  Widget _badge(String text, String tooltip) => Tooltip(
    message: tooltip,
    child: DecoratedBox(
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: .75),
        borderRadius: BorderRadius.circular(5),
      ),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 3),
        child: Text(
          text,
          style: const TextStyle(
            color: Colors.white,
            fontSize: 12,
            height: 1.2,
          ),
        ),
      ),
    ),
  );
}
