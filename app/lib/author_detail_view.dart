import 'package:flutter/material.dart';
import 'api.dart';
import 'api_image.dart';
import 'author_download_controls.dart';
import 'item_cover.dart';
import 'models.dart';

typedef AuthorFilterChanged =
    void Function({String? section, String? category, String? restrict});

/// The native author page presentation; requests remain owned by AuthorScreen.
class AuthorDetailView extends StatefulWidget {
  final LibraryApi api;
  final String? authorId, error;
  final Map<String, dynamic>? profile, feed;
  final List<LibraryItem> items;
  final String section, category, restrict;
  final int page;
  final bool loading, readingFeed, pending, profilePending;
  final VoidCallback onRefreshProfile, onQueueFeed;
  final Future<void> Function() onRefreshFeed, onLoadMore;
  final ValueChanged<String> onOpenItem;
  final AuthorFilterChanged onChoose;

  const AuthorDetailView({
    super.key,
    required this.api,
    required this.authorId,
    required this.profile,
    required this.feed,
    required this.items,
    required this.section,
    required this.category,
    required this.restrict,
    required this.page,
    required this.loading,
    required this.readingFeed,
    required this.pending,
    required this.profilePending,
    required this.onRefreshProfile,
    required this.onQueueFeed,
    required this.onRefreshFeed,
    required this.onLoadMore,
    required this.onOpenItem,
    required this.onChoose,
    this.error,
  });

  @override
  State<AuthorDetailView> createState() => _AuthorDetailViewState();
}

class _AuthorDetailViewState extends State<AuthorDetailView> {
  bool _fullDescription = false;

  @override
  void didUpdateWidget(covariant AuthorDetailView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.authorId != widget.authorId) _fullDescription = false;
  }

  Widget _biography(BuildContext context, String description) {
    final style = Theme.of(context).textTheme.bodyMedium?.copyWith(
      height: 1.6,
      color: Theme.of(context).colorScheme.onSurfaceVariant,
    );
    return LayoutBuilder(
      builder: (context, constraints) {
        final measure = TextPainter(
          text: TextSpan(text: description, style: style),
          maxLines: 3,
          textDirection: Directionality.of(context),
          textScaler: MediaQuery.textScalerOf(context),
          locale: Localizations.maybeLocaleOf(context),
        )..layout(maxWidth: constraints.maxWidth);
        final overflow = measure.didExceedMaxLines;
        measure.dispose();
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              description,
              maxLines: _fullDescription ? null : 3,
              overflow: _fullDescription ? null : TextOverflow.ellipsis,
              style: style,
            ),
            if (overflow || _fullDescription)
              TextButton(
                style: TextButton.styleFrom(padding: EdgeInsets.zero),
                onPressed: () =>
                    setState(() => _fullDescription = !_fullDescription),
                child: Text(_fullDescription ? '收起简介' : '展开简介'),
              ),
          ],
        );
      },
    );
  }

  Widget _profileCard(BuildContext context) {
    final profile = widget.profile!;
    final colors = Theme.of(context).colorScheme;
    final text = Theme.of(context).textTheme;
    final description = (profile['description'] as String? ?? '').trim();
    return Material(
      color: colors.surface,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(24),
        side: BorderSide(color: colors.outlineVariant.withValues(alpha: .6)),
      ),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox.square(
                  dimension: 68,
                  child: ClipOval(
                    child: profile['avatar_url'] == null
                        ? CircleAvatar(
                            backgroundColor: colors.primaryContainer,
                            foregroundColor: colors.onPrimaryContainer,
                            child: const Icon(Icons.person_outline, size: 34),
                          )
                        : ApiImage(
                            api: widget.api,
                            path: profile['avatar_url'] as String,
                          ),
                  ),
                ),
                const SizedBox(width: 16),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        profile['name'] as String? ?? 'Pixiv 作者',
                        style: text.titleLarge?.copyWith(
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      const SizedBox(height: 6),
                      Text(
                        'Pixiv · ID ${widget.authorId}',
                        style: text.bodySmall?.copyWith(
                          color: colors.onSurfaceVariant,
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
            if (description.isNotEmpty) ...[
              const SizedBox(height: 16),
              _biography(context, description),
            ],
            if (widget.authorId != null &&
                profile['download_settings'] is Map) ...[
              const SizedBox(height: 12),
              const Divider(height: 1),
              Theme(
                data: Theme.of(
                  context,
                ).copyWith(dividerColor: Colors.transparent),
                child: ExpansionTile(
                  dense: true,
                  key: PageStorageKey('author-download-${widget.authorId}'),
                  tilePadding: EdgeInsets.zero,
                  childrenPadding: const EdgeInsets.only(bottom: 4),
                  leading: Icon(
                    Icons.tune_rounded,
                    size: 20,
                    color: colors.onSurfaceVariant,
                  ),
                  title: Text('自动下载设置', style: text.labelLarge),
                  children: [
                    Align(
                      alignment: Alignment.centerLeft,
                      child: AuthorDownloadControls(
                        api: widget.api,
                        authorId: widget.authorId!,
                        initial: Map<String, dynamic>.from(
                          profile['download_settings'] as Map,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _filters(BuildContext context, int total) {
    final colors = Theme.of(context).colorScheme;
    final text = Theme.of(context).textTheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SegmentedButton<String>(
          showSelectedIcon: false,
          expandedInsets: EdgeInsets.zero,
          segments: const [
            ButtonSegment(
              value: 'works',
              icon: Icon(Icons.grid_view_rounded),
              label: Text('作者作品'),
            ),
            ButtonSegment(
              value: 'bookmarks',
              icon: Icon(Icons.bookmarks_outlined),
              label: Text('收藏'),
            ),
          ],
          selected: {widget.section},
          onSelectionChanged: (value) => widget.onChoose(section: value.first),
        ),
        const SizedBox(height: 12),
        Wrap(
          spacing: 8,
          runSpacing: 4,
          children: [
            for (final entry in const {
              'illustration': '插画',
              'manga': '漫画',
              'novel': '小说',
            }.entries)
              ChoiceChip(
                showCheckmark: false,
                label: Text(entry.value),
                selected: widget.category == entry.key,
                onSelected: (_) => widget.onChoose(category: entry.key),
              ),
            if (widget.section == 'bookmarks' &&
                widget.profile?['can_read_private'] == true)
              ChoiceChip(
                label: const Text('私密收藏'),
                selected: widget.restrict == 'private',
                onSelected: (value) =>
                    widget.onChoose(restrict: value ? 'private' : 'public'),
              ),
          ],
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            Expanded(
              child: Text(
                '${widget.feed?['local_only'] == true ? '本地已收录' : '已读取'} $total 部',
                style: text.bodySmall?.copyWith(color: colors.onSurfaceVariant),
              ),
            ),
            TextButton.icon(
              onPressed:
                  widget.authorId == null || widget.pending || widget.loading
                  ? null
                  : widget.onQueueFeed,
              icon: const Icon(Icons.sync_rounded, size: 17),
              label: const Text('重新读取目录'),
            ),
          ],
        ),
        if (widget.feed?['complete'] == true)
          Text(
            '目录已完整读取',
            style: text.bodySmall?.copyWith(color: colors.onSurfaceVariant),
          ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    final total = (widget.feed?['total'] as num? ?? 0).toInt();
    final more = widget.page * 24 < total || widget.feed?['has_more'] == true;
    final busy = widget.pending || widget.readingFeed;
    return Scaffold(
      backgroundColor: colors.surfaceContainerLowest,
      appBar: AppBar(
        backgroundColor: colors.surfaceContainerLowest,
        title: const Text('作者详情'),
        actions: [
          IconButton(
            tooltip: '刷新作者资料',
            onPressed: widget.loading ? null : widget.onRefreshProfile,
            icon: const Icon(Icons.refresh_rounded),
          ),
          const SizedBox(width: 8),
        ],
      ),
      body: SafeArea(
        top: false,
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 1000),
            child: LayoutBuilder(
              builder: (context, box) {
                final scale = MediaQuery.textScalerOf(context).scale(14) / 14;
                final columns = box.maxWidth < 340 || scale >= 1.6
                    ? 1
                    : box.maxWidth >= 900
                    ? 4
                    : box.maxWidth >= 600
                    ? 3
                    : 2;
                final cardWidth =
                    (box.maxWidth - 32 - 12 * (columns - 1)) / columns;
                return NotificationListener<ScrollNotification>(
                  onNotification: (event) {
                    if (event is ScrollUpdateNotification &&
                        event.metrics.extentAfter < 300 &&
                        widget.error == null) {
                      widget.onLoadMore();
                    }
                    return false;
                  },
                  child: RefreshIndicator(
                    onRefresh: widget.onRefreshFeed,
                    child: CustomScrollView(
                      key: PageStorageKey(
                        'author-${widget.authorId}-${widget.section}-${widget.category}-${widget.restrict}',
                      ),
                      physics: const AlwaysScrollableScrollPhysics(),
                      slivers: [
                        SliverPadding(
                          padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
                          sliver: SliverToBoxAdapter(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              children: [
                                if (widget.profile != null) ...[
                                  _profileCard(context),
                                  const SizedBox(height: 20),
                                ],
                                _filters(context, total),
                                if (widget.pending || widget.profilePending)
                                  const _AuthorNotice(
                                    icon: Icons.schedule_rounded,
                                    message: '正在服务器队列中读取；采集暂停时，请在网页任务页恢复。',
                                  ),
                                if ((widget.feed?['unavailable'] as num? ?? 0) >
                                    0)
                                  const _AuthorNotice(
                                    icon: Icons.info_outline_rounded,
                                    message: '含不可访问或暂不支持的条目，目录可能不完整。',
                                  ),
                                if (widget.error != null)
                                  _AuthorNotice(
                                    icon: Icons.error_outline_rounded,
                                    message: widget.error!,
                                    error: true,
                                  ),
                                if (widget.loading || widget.readingFeed) ...[
                                  const SizedBox(height: 8),
                                  const LinearProgressIndicator(minHeight: 2),
                                ],
                                const SizedBox(height: 12),
                              ],
                            ),
                          ),
                        ),
                        if (widget.items.isEmpty)
                          SliverFillRemaining(
                            hasScrollBody: false,
                            child: Padding(
                              padding: const EdgeInsets.all(32),
                              child: Column(
                                mainAxisAlignment: MainAxisAlignment.center,
                                children: [
                                  Icon(
                                    Icons.collections_bookmark_outlined,
                                    size: 44,
                                    color: colors.outline,
                                  ),
                                  const SizedBox(height: 16),
                                  Text(
                                    widget.loading || widget.readingFeed
                                        ? '正在读取作品…'
                                        : '目录尚无可显示内容',
                                    style: TextStyle(
                                      color: colors.onSurfaceVariant,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          )
                        else
                          SliverPadding(
                            padding: const EdgeInsets.symmetric(horizontal: 16),
                            sliver: SliverGrid(
                              gridDelegate:
                                  SliverGridDelegateWithFixedCrossAxisCount(
                                    crossAxisCount: columns,
                                    crossAxisSpacing: 12,
                                    mainAxisSpacing: 16,
                                    mainAxisExtent:
                                        cardWidth * 1.25 + 124 * scale,
                                  ),
                              delegate: SliverChildBuilderDelegate((
                                context,
                                index,
                              ) {
                                final item = widget.items[index];
                                return _AuthorWorkCard(
                                  key: ValueKey(item.id),
                                  api: widget.api,
                                  item: item,
                                  onTap: () => widget.onOpenItem(item.id),
                                );
                              }, childCount: widget.items.length),
                            ),
                          ),
                        if (widget.items.isNotEmpty && (more || busy))
                          SliverToBoxAdapter(
                            child: Padding(
                              padding: const EdgeInsets.all(20),
                              child: Center(
                                child: busy
                                    ? const SizedBox.square(
                                        dimension: 22,
                                        child: CircularProgressIndicator(
                                          strokeWidth: 2,
                                        ),
                                      )
                                    : TextButton(
                                        onPressed: widget.loading
                                            ? null
                                            : widget.onLoadMore,
                                        child: Text(
                                          widget.error != null
                                              ? '加载失败，重试'
                                              : '加载更多',
                                        ),
                                      ),
                              ),
                            ),
                          ),
                        const SliverToBoxAdapter(child: SizedBox(height: 24)),
                      ],
                    ),
                  ),
                );
              },
            ),
          ),
        ),
      ),
    );
  }
}

class _AuthorNotice extends StatelessWidget {
  final IconData icon;
  final String message;
  final bool error;
  const _AuthorNotice({
    required this.icon,
    required this.message,
    this.error = false,
  });
  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    return Container(
      margin: const EdgeInsets.only(top: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: error ? colors.errorContainer : colors.surfaceContainer,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(
            icon,
            size: 18,
            color: error ? colors.onErrorContainer : colors.onSurfaceVariant,
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              message,
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                height: 1.5,
                color: error
                    ? colors.onErrorContainer
                    : colors.onSurfaceVariant,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _AuthorWorkCard extends StatelessWidget {
  final LibraryApi api;
  final LibraryItem item;
  final VoidCallback onTap;
  const _AuthorWorkCard({
    super.key,
    required this.api,
    required this.item,
    required this.onTap,
  });
  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    final scale = MediaQuery.textScalerOf(context).scale(14) / 14;
    final type = switch (item.type) {
      'novel' || 'article' => '小说',
      'illustration' => '插画',
      _ => '漫画',
    };
    final state = switch (item.contentState) {
      'ready' => '已下载',
      'downloading' => '下载中',
      'queued' => '等待下载',
      'failed' => '下载失败',
      _ => '正文未下载',
    };
    return Card(
      margin: EdgeInsets.zero,
      elevation: 0,
      color: colors.surface,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(16),
        side: BorderSide(color: colors.outlineVariant.withValues(alpha: .5)),
      ),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: SizedBox(
                width: double.infinity,
                child: ItemCover(api: api, item: item),
              ),
            ),
            Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  SizedBox(
                    height: 42 * scale,
                    child: Text(
                      item.displayTitle,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        fontSize: 14,
                        height: 1.4,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  const SizedBox(height: 6),
                  Text(
                    item.author.isEmpty ? 'Pixiv' : item.author,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: 12,
                      color: colors.onSurfaceVariant,
                    ),
                  ),
                  const SizedBox(height: 6),
                  Text(
                    '$type · $state',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: 11,
                      color: item.contentState == 'ready'
                          ? colors.primary
                          : colors.onSurfaceVariant,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
