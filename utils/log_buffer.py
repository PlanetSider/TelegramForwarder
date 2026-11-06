"""进程内日志环形缓冲。

用途：Web 控制台的「日志与状态」页需要在浏览器里看到运行日志，
但项目原本只挂 StreamHandler 输出到 stdout（docker logs 才能看到）。

这里加一个内存 deque 作为 log handler，零磁盘 IO；
容量固定，写满自动淘汰最旧记录。
"""
import logging
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

_DEFAULT_CAPACITY = 2000


class RingBufferHandler(logging.Handler):
    """把日志记录塞进定长 deque，供 HTTP 接口读取。"""

    def __init__(self, capacity: int = _DEFAULT_CAPACITY):
        super().__init__()
        self._buffer: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{self.format(record)}"
            item = {
                "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "epoch": record.created,
                "level": record.levelname,
                "name": record.name,
                "msg": message,
            }
            with self._lock:
                self._buffer.append(item)
        except Exception:  # 日志处理器自身绝不能抛异常
            pass

    def snapshot(self, limit: int = 300,
                 level: Optional[str] = None,
                 keyword: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._buffer)
        if level:
            lv = level.upper()
            items = [i for i in items if i["level"] == lv]
        if keyword:
            kw = keyword.lower()
            items = [i for i in items if kw in i["msg"].lower()
                     or kw in i["name"].lower()]
        return items[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()


_ring_handler: Optional[RingBufferHandler] = None


def setup_ring_buffer(capacity: int = _DEFAULT_CAPACITY) -> RingBufferHandler:
    """幂等地挂载环形缓冲到 root logger。"""
    global _ring_handler
    if _ring_handler is not None:
        return _ring_handler

    handler = RingBufferHandler(capacity)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(name)s - %(levelname)s - %(message)s"))

    root = logging.getLogger()
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    root.addHandler(handler)
    _ring_handler = handler
    return handler


def get_ring_buffer() -> Optional[RingBufferHandler]:
    return _ring_handler


def get_recent(limit: int = 300,
               level: Optional[str] = None,
               keyword: Optional[str] = None) -> List[Dict[str, Any]]:
    handler = _ring_handler
    if handler is None:
        return []
    return handler.snapshot(limit=limit, level=level, keyword=keyword)
