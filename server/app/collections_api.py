"""Read-only summaries for the five physically separated library collections."""
from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select

from .auth import require_auth
from .database import get_db
from .models import Asset, Item
from .storage import COLLECTIONS

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/library/collections")
def list_collections(db=Depends(get_db)):
    items = dict(db.execute(select(Item.source, func.count(Item.id)).group_by(Item.source)).all())
    saved = dict(db.execute(select(Item.source, func.count(Item.id)).where(Item.assets.any()).group_by(Item.source)).all())
    rows = db.execute(select(Item.source, func.count(Asset.id), func.sum(Asset.size_bytes),
        func.sum(case((Asset.relative_path.not_like("collections/" + Item.source + "/blobs/%"), 1), else_=0)))
        .join(Asset, Asset.item_id == Item.id).group_by(Item.source)).all()
    assets = {source: (pages, size, legacy) for source, pages, size, legacy in rows}
    output = []
    for source, name in COLLECTIONS.items():
        pages, size, legacy = assets.get(source, (0, 0, 0))
        total, downloaded = items.get(source, 0), saved.get(source, 0)
        output.append({"source": source, "name": name, "storage_directory": f"collections/{source}",
            "item_count": total, "saved_item_count": downloaded, "metadata_item_count": total - downloaded,
            "page_count": pages, "size_bytes": int(size or 0), "legacy_asset_count": int(legacy or 0)})
    return output
