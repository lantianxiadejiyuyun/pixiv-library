"""Separate authored works and public bookmarks while preserving legacy settings."""
def flags(subscription):
    return {key:bool(getattr(subscription,key) if getattr(subscription,key) is not None else subscription.auto_download)
            for key in ("download_works","download_bookmarks")}


def wants_body(subscription, remote_id, origins=None):
    if subscription.platform != "pixiv" or subscription.target_type != "author":
        return subscription.auto_download
    options = flags(subscription)
    categories = (origins or {}).get(remote_id)
    if categories is None:
        return subscription.auto_download if subscription.download_works is None and subscription.download_bookmarks is None else False
    return bool("works" in categories and options["download_works"] or "bookmarks" in categories and options["download_bookmarks"])
