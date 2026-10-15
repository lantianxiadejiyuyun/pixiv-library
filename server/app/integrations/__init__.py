"""Local connector registry. Capability flags reflect implemented code, not promises."""
from .base import ConnectorError, RemoteWork


def get_connector(platform, credentials=None):
    from .pixiv import PixivConnector
    from .ehentai import EHentaiConnector
    from .jm import JMConnector
    from .picacg import PicACGConnector
    classes = {"pixiv": PixivConnector, "ehentai": EHentaiConnector,
               "jm": JMConnector, "picacg": PicACGConnector}
    if platform not in classes:
        raise ConnectorError("未知内容平台")
    return classes[platform](credentials or {})


PLATFORMS = {"pixiv": "Pixiv", "ehentai": "E-Hentai", "picacg": "哔咔 PicACG", "jm": "JM 天堂"}
