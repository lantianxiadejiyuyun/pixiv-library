import 'publication_time.dart';
import 'novel_blocks.dart';

class ReadingPosition {
  final int page, section;
  final double offset;
  const ReadingPosition({this.page = 0, this.section = 0, this.offset = 0});
  factory ReadingPosition.fromJson(Map<String, dynamic>? json) =>
      ReadingPosition(
        page: (json?['page'] as num?)?.toInt() ?? 0,
        section: (json?['section'] as num?)?.toInt() ?? 0,
        offset: (json?['offset'] as num?)?.toDouble() ?? 0,
      );
  Map<String, dynamic> toJson() => {
    'page': page,
    'section': section,
    'offset': offset,
  };
}

class LibraryAsset {
  final String id, mimeType, name;
  final String? inlineKey;
  final int position;
  LibraryAsset.fromJson(Map<String, dynamic> json)
    : id = json['id'] as String,
      inlineKey = json['inline_key'] as String?,
      mimeType = json['mime_type'] as String? ?? '',
      name = json['original_name'] as String? ?? '',
      position = (json['position'] as num?)?.toInt() ?? 0;
}

class SeriesLink {
  final String id, title;
  SeriesLink.fromJson(Map<String, dynamic> json)
    : id = json['id'] as String,
      title = json['title'] as String? ?? '系列';
}

class TextChapter {
  final int index;
  final String title, text;
  final List<NovelBlock>? blocks;
  TextChapter.fromJson(Map<String, dynamic> json)
    : index = (json['index'] as num).toInt(),
      title = json['title'] as String? ?? '',
      text = json['text'] as String? ?? '',
      blocks = (json['blocks'] as List?)
          ?.map((v) => NovelBlock.fromJson(v as Map<String, dynamic>))
          .toList(growable: false);
}

class LibraryTag {
  final String id, name;
  final String? nameZh;
  final int count;
  LibraryTag.fromJson(Map<String, dynamic> json)
    : id = json['id'] as String? ?? json['name'] as String,
      name = json['name'] as String,
      nameZh = json['name_zh'] as String?,
      count = (json['count'] as num?)?.toInt() ?? 0;
  String get displayName => nameZh?.isNotEmpty == true ? nameZh! : name;
}

class LibraryItem {
  final String rating;
  final String id, title, type, source, author, description, contentState;
  final String? coverUrl;
  final String? titleZh;
  final String? authorSourceId;
  final String? sourceId;
  final DateTime? sourcePublishedAt;
  final int pageCount;
  final bool isFavorite;
  final int userScore;
  final int? sourceBookmarkCount;
  final List<String> favoriteGroupIds;
  final List<LibraryAsset> assets;
  late final List<LibraryAsset> readingAssets = assets
      .where((asset) => asset.inlineKey == null)
      .toList(growable: false);
  final List<String> tags;
  final List<LibraryTag> tagEntries;
  final ReadingPosition progress;
  final List<SeriesLink> series;
  LibraryItem.fromJson(Map<String, dynamic> json)
    : rating = json['rating'] as String? ?? 'unknown',
      id = json['id'] as String,
      title = json['title'] as String? ?? '未命名',
      titleZh = json['title_zh'] as String?,
      authorSourceId = json['author_source_id'] as String?,
      sourceId = json['source_id'] as String?,
      sourcePublishedAt = parsePublicationTime(json['source_published_at']),
      type = json['type'] as String? ?? 'manga',
      source = json['source'] as String? ?? 'local',
      author = json['author'] as String? ?? '',
      description = json['description'] as String? ?? '',
      contentState = json['content_state'] as String? ?? 'metadata',
      coverUrl = json['cover_url'] as String?,
      pageCount = (json['page_count'] as num?)?.toInt() ?? 0,
      isFavorite = json['is_favorite'] == true,
      userScore = (json['user_score'] as num?)?.toInt() ?? 0,
      sourceBookmarkCount = (json['source_bookmark_count'] as num?)?.toInt(),
      favoriteGroupIds = List<String>.from(
        json['favorite_group_ids'] as List? ?? [],
      ),
      assets =
          (json['assets'] as List? ?? [])
              .map((v) => LibraryAsset.fromJson(v as Map<String, dynamic>))
              .toList()
            ..sort((a, b) => a.position.compareTo(b.position)),
      tags = (json['tags'] as List? ?? [])
          .map((v) => (v as Map)['name'] as String)
          .toList(),
      tagEntries = (json['tags'] as List? ?? [])
          .map((v) => LibraryTag.fromJson(v as Map<String, dynamic>))
          .toList(),
      progress = ReadingPosition.fromJson(
        json['progress'] as Map<String, dynamic>?,
      ),
      series = (json['series'] as List? ?? [])
          .map((v) => SeriesLink.fromJson(v as Map<String, dynamic>))
          .toList();
  bool get readable => contentState == 'ready' && assets.isNotEmpty;
  LibraryItem withProgress(ReadingPosition position) =>
      LibraryItem._copy(this, progress: position);
  LibraryItem withFavorite(Map<String, dynamic> value) => LibraryItem._copy(
    this,
    favorite: value['is_favorite'] as bool,
    groups: List<String>.from(value['favorite_group_ids'] as List),
  );
  LibraryItem withScore(int score) => LibraryItem._copy(this, score: score);
  LibraryItem._copy(
    LibraryItem item, {
    ReadingPosition? progress,
    bool? favorite,
    List<String>? groups,
    int? score,
  }) : rating = item.rating,
       id = item.id,
       title = item.title,
       type = item.type,
       source = item.source,
       author = item.author,
       description = item.description,
       contentState = item.contentState,
       coverUrl = item.coverUrl,
       titleZh = item.titleZh,
       authorSourceId = item.authorSourceId,
       sourceId = item.sourceId,
       sourcePublishedAt = item.sourcePublishedAt,
       pageCount = item.pageCount,
       isFavorite = favorite ?? item.isFavorite,
       userScore = score ?? item.userScore,
       sourceBookmarkCount = item.sourceBookmarkCount,
       favoriteGroupIds = groups ?? item.favoriteGroupIds,
       progress = progress ?? item.progress,
       assets = item.assets,
       tags = item.tags,
       tagEntries = item.tagEntries,
       series = item.series;
  String get displayTitle => titleZh?.isNotEmpty == true ? titleZh! : title;
  bool get isText => type == 'novel' || type == 'article';
}

class WallCharacter {
  final String id, name, displayName, tagName;
  final int count;
  final List<Map<String, dynamic>> covers;
  final Map<String, dynamic>? profile;
  WallCharacter.fromJson(Map<String, dynamic> json)
    : id = json['id'] as String,
      name = json['name'] as String? ?? '',
      displayName =
          json['display_name'] as String? ?? json['name'] as String? ?? '',
      tagName = json['tag_name'] as String? ?? '',
      count = (json['count'] as num?)?.toInt() ?? 0,
      covers = (json['covers'] as List? ?? [])
          .map((v) => Map<String, dynamic>.from(v as Map))
          .toList(),
      profile = json['profile'] == null
          ? null
          : Map<String, dynamic>.from(json['profile'] as Map);
}
