"""Separate authored works and public bookmarks while preserving legacy settings."""
def flags(subscription, *, default_works=False):
    options = {key:bool(getattr(subscription,key) if getattr(subscription,key) is not None else subscription.auto_download)
               for key in ("download_works","download_bookmarks")}
    if subscription.platform == 'pixiv' and subscription.target_type == 'author' and subscription.download_works is None:
        options['download_works'] = bool(subscription.auto_download or default_works)
    return options


def wants_body(subscription, remote_id, origins=None, *, default_works=False):
    if subscription.platform != "pixiv" or subscription.target_type != "author":
        return subscription.auto_download
    options = flags(subscription, default_works=default_works)
    categories = (origins or {}).get(remote_id)
    if categories is None:
        return subscription.auto_download if subscription.download_works is None and subscription.download_bookmarks is None else False
    return bool("works" in categories and options["download_works"] or "bookmarks" in categories and options["download_bookmarks"])
