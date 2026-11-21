"""规则占用空间统计与清理。

每个规则的数据分两块：
  * 数据库里的转发历史（forward_history，kind='rule' + ref_id=规则 id）；
  * 磁盘 RSS_MEDIA_DIR/<rule_id>/ 下的媒体文件，按图片/视频/其他分类。

扫描媒体目录要遍历文件，列表页会把所有规则一起拉一遍，所以结果缓存 60 秒。
清理后立刻失效缓存，页面能看到真实变化。
"""
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

CACHE_TTL = 60.0
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif", ".svg", ".avif"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v", ".ts", ".wmv", ".mpeg", ".mpg"}

_cache: Dict[int, Any] = {}


def media_root(rule_id: int) -> str:
    from utils.constants import RSS_MEDIA_DIR
    return os.path.join(RSS_MEDIA_DIR, str(rule_id))


def invalidate(rule_id: Optional[int] = None) -> None:
    if rule_id is None:
        _cache.clear()
    else:
        _cache.pop(int(rule_id), None)


def _history_sizes(session, rule_id: int):
    """返回 (行数, content 字节, detail 字节)。"""
    from sqlalchemy import text

    try:
        row = session.execute(text(
            "SELECT COUNT(*), "
            "COALESCE(SUM(LENGTH(CAST(content AS BLOB))), 0), "
            "COALESCE(SUM(LENGTH(CAST(detail AS BLOB))), 0) "
            "FROM forward_history WHERE kind = 'rule' AND ref_id = :rid"
        ), {"rid": int(rule_id)}).fetchone()
        return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    except Exception:
        # 非 SQLite 方言或表尚未建立：退回按字符数估算
        try:
            session.rollback()
        except Exception:
            pass
        from sqlalchemy import func
        from models.models import ForwardHistory
        rows, content_len, detail_len = (
            session.query(
                func.count(ForwardHistory.id),
                func.coalesce(func.sum(func.length(ForwardHistory.content)), 0),
                func.coalesce(func.sum(func.length(ForwardHistory.detail)), 0),
            )
            .filter(ForwardHistory.kind == "rule", ForwardHistory.ref_id == rule_id)
            .one()
        )
        return int(rows or 0), int(content_len or 0), int(detail_len or 0)


def scan_media(rule_id: int) -> Dict[str, Any]:
    root = media_root(rule_id)
    buckets = {
        "images": {"bytes": 0, "count": 0},
        "videos": {"bytes": 0, "count": 0},
        "others": {"bytes": 0, "count": 0},
    }
    if not os.path.isdir(root):
        return {
            "path": root,
            "exists": False,
            "total": 0,
            "files": 0,
            **buckets,
        }

    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            path = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if ext in IMAGE_EXTS:
                bucket = "images"
            elif ext in VIDEO_EXTS:
                bucket = "videos"
            else:
                bucket = "others"
            buckets[bucket]["bytes"] += size
            buckets[bucket]["count"] += 1

    total = sum(b["bytes"] for b in buckets.values())
    files = sum(b["count"] for b in buckets.values())
    return {"path": root, "exists": True, "total": total, "files": files, **buckets}


def rule_storage(session, rule_id: int, use_cache: bool = True) -> Dict[str, Any]:
    """单条规则的占用空间统计。"""
    now = time.time()
    if use_cache:
        hit = _cache.get(int(rule_id))
        if hit and now - hit["cached_at"] < CACHE_TTL:
            return hit

    rows, content_bytes, detail_bytes = _history_sizes(session, rule_id)
    media = scan_media(rule_id)
    db_bytes = content_bytes + detail_bytes
    payload = {
        "rule_id": int(rule_id),
        "total": db_bytes + media["total"],
        "database": {
            "bytes": db_bytes,
            "rows": rows,
            "content_bytes": content_bytes,
            "detail_bytes": detail_bytes,
        },
        "media": media,
        "cached_at": now,
    }
    _cache[int(rule_id)] = payload
    return payload


def many_storage(session, rule_ids: Iterable[int]) -> List[Dict[str, Any]]:
    return [rule_storage(session, rid) for rid in rule_ids]


def _stamp_media_cleared(session, rule_id: int, kind: str) -> None:
    """按类型记录清理时间：图片与视频各自独立，互不影响。"""
    from models.models import ForwardRule

    rule = session.query(ForwardRule).get(rule_id)
    if rule is None:
        return
    field = "images_cleared_at" if kind == "images" else "videos_cleared_at"
    setattr(rule, field, datetime.utcnow())
    session.commit()


def clear_history(session, rule_id: int) -> Dict[str, Any]:
    """删掉该规则的转发历史，返回释放字节数。"""
    from models.models import ForwardHistory

    _rows, content_bytes, detail_bytes = _history_sizes(session, rule_id)
    removed = (
        session.query(ForwardHistory)
        .filter(ForwardHistory.kind == "rule", ForwardHistory.ref_id == rule_id)
        .delete(synchronize_session=False)
    )
    session.commit()
    invalidate(rule_id)
    return {"removed": int(removed or 0), "freed": content_bytes + detail_bytes}


def clear_media(session, rule_id: int, kind: str) -> Dict[str, Any]:
    """删掉图片或视频文件。kind 只接受 images / videos。

    清掉之后只更新该类型自己的清理时间（images_cleared_at / videos_cleared_at），
    RSS 过滤器据此只下载该类型清理之后的新媒体，另一类不受影响。
    """
    if kind not in ("images", "videos"):
        raise ValueError("只能清理 images 或 videos")
    exts = IMAGE_EXTS if kind == "images" else VIDEO_EXTS
    root = media_root(rule_id)
    freed = 0
    removed = 0
    if os.path.isdir(root):
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if os.path.splitext(name)[1].lower() not in exts:
                    continue
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                    os.remove(path)
                except OSError:
                    continue
                freed += size
                removed += 1
    if removed:
        _stamp_media_cleared(session, rule_id, kind)
    invalidate(rule_id)
    return {"removed": removed, "freed": freed}
