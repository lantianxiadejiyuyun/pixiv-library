/// An inert reading view. Original text and marker positions remain unchanged.
class NovelBlock {
  final String type, text, key;
  final String? url;
  final double? width, height;
  const NovelBlock({
    this.type = 'text',
    this.text = '',
    this.key = '',
    this.url,
    this.width,
    this.height,
  });

  factory NovelBlock.fromJson(Map<String, dynamic> value) => NovelBlock(
    type: value['type'] == 'image' ? 'image' : 'text',
    text: value['text'] as String? ?? '',
    key: value['key'] as String? ?? '',
    url: value['url'] as String?,
    width: (value['width'] as num?)?.toDouble(),
    height: (value['height'] as num?)?.toDouble(),
  );

  double get aspectRatio =>
      width != null &&
          height != null &&
          width!.isFinite &&
          height!.isFinite &&
          width! > 0 &&
          height! > 0
      ? (width! / height!).clamp(.02, 100)
      : .75;
}

class NovelDocument {
  final String text;
  final List<NovelBlock> blocks;
  const NovelDocument(this.text, this.blocks);
}

final novelImageMarker = RegExp(
  r'\[(uploadedimage:[1-9]\d{0,19}|pixivimage:[1-9]\d{0,19}(?:-[1-9]\d{0,3})?)\]',
);

List<NovelBlock> novelBlocks(String text, Map<String, String> images) {
  final blocks = <NovelBlock>[];
  var start = 0;
  for (final marker in novelImageMarker.allMatches(text)) {
    if (marker.start > start) {
      blocks.add(NovelBlock(text: text.substring(start, marker.start)));
    }
    final key = marker[1]!;
    blocks.add(NovelBlock(type: 'image', key: key, url: images[key]));
    start = marker.end;
  }
  if (start < text.length || blocks.isEmpty) {
    blocks.add(NovelBlock(text: text.substring(start)));
  }
  return List.unmodifiable(blocks);
}
