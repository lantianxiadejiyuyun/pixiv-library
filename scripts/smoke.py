"""Exercise a running instance using temporary, clearly labelled test content.

Set LIBRARY_SMOKE_PASSWORD (defaults to the loopback development password).
Created library records are removed in finally; immutable test blobs remain safe.
"""
import argparse
import io
import os
import time
import zipfile

import httpx
from PIL import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--ai", action="store_true")
    args = parser.parse_args()
    item_ids = []
    with httpx.Client(base_url=args.url, timeout=60) as client:
        def request(method, path, **kwargs):
            response = client.request(method, "/api" + path, **kwargs)
            response.raise_for_status()
            return response
        assert request("GET", "/health").json()["status"] == "ok"
        assert client.get("/api/items").status_code == 401
        request("POST", "/auth/login", json={"password": os.getenv("LIBRARY_SMOKE_PASSWORD", "local-library")})
        try:
            pages = []
            for index, color in [(10, "#d9dfd4"), (2, "#eee6d5")]:
                image = io.BytesIO()
                Image.new("RGB", (160, 240), color).save(image, "PNG")
                pages.append((f"page{index}.png", image.getvalue()))
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as book:
                for name, content in pages:
                    book.writestr(name, content)
            item = request("POST", "/imports/upload", data={"title": "__smoke__ 图书验证", "type": "manga"},
                           files={"files": ("smoke.cbz", archive.getvalue(), "application/zip")}).json()
            item_ids.append(item["id"])
            assert [a["original_name"] for a in item["assets"]] == ["page2.png", "page10.png"]
            assert item["page_count"] == 2
            assert client.get(item["cover_url"]).headers["content-type"].startswith("image/")
            assert client.get(item["assets"][0]["url"]).content == pages[1][1]
            request("PATCH", f"/items/{item['id']}", json={"tags": ["验证标签", "角色:测试"], "rating": "sensitive"})
            assert request("GET", "/items", params={"tag": "验证标签"}).json()["total"] >= 1
            request("PUT", f"/items/{item['id']}/progress", json={"page": 1})
            assert request("GET", f"/items/{item['id']}").json()["progress"]["page"] == 1
            exported = request("GET", f"/items/{item['id']}/export")
            with zipfile.ZipFile(io.BytesIO(exported.content)) as book:
                assert len(book.namelist()) == 2
            text = request("POST", "/imports/upload", data={"title": "__smoke__ 文章验证", "type": "article"},
                           files={"files": ("article.md", "# 数据库学习\nPython 编程与算法。".encode(), "text/plain")}).json()
            item_ids.append(text["id"])
            targets = item_ids if args.ai else [text["id"]]
            jobs = request("POST", "/ai/analyze", json={"item_ids": targets}).json()["jobs"]
            wanted = {j["id"] for j in jobs}
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                rows = [j for j in request("GET", "/jobs").json()["items"] if j["id"] in wanted]
                if all(j["status"] in ("completed", "failed", "cancelled") for j in rows):
                    assert all(j["status"] == "completed" for j in rows), rows
                    break
                time.sleep(0.5)
            else:
                raise AssertionError("AI job did not finish")
            detail = request("GET", f"/items/{item['id']}").json()
            assert detail["rating"] == "sensitive", "AI overwrote a manual rating"
            assert "验证标签" in [t["name"] for t in detail["tags"]]
            assert "分类:科技" in [t["name"] for t in request("GET", f"/items/{text['id']}").json()["tags"]]
            for route in ("/dashboard", "/sources", "/subscriptions", "/tags", "/ai/status", "/settings"):
                request("GET", route)
            print("PASS: login, archive import/order, content, thumbnails, edit, filters, progress, export, text classification, manual override, management endpoints" + (", real local ONNX jobs" if args.ai else ""))
        finally:
            for item_id in item_ids:
                request("DELETE", f"/items/{item_id}")
            request("POST", "/auth/logout")
            assert client.get("/api/items").status_code == 401


if __name__ == "__main__":
    main()
