"""config/ 目录下文本类配置的读写。

这些文件是运行时可变的“热配置”：
- `summary_times.txt` / `delay_times.txt` / `max_media_size.txt` / `media_extensions.txt`
  是「一行一个候选值」的纯文本，供 Bot 的分页按钮读取；
- `ai_models.json` 是 `{provider: [model, ...]}` 结构。

写入只允许白名单文件，且必须先通过内容校验，
避免控制台变成一个可以往仓库里写任意文件的入口。
"""
import json
import logging
import os
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

CONFIG_DIR = "config"

ALLOWED_FILES = {
    "summary_times.txt": "总结时间（HH:MM，一行一个）",
    "delay_times.txt": "延时秒数（一行一个整数）",
    "max_media_size.txt": "媒体大小上限 MB（一行一个数字）",
    "media_extensions.txt": "媒体扩展名（一行一个，不带点）",
    "ai_models.json": "AI 模型清单（JSON: provider -> [模型]）",
}


def _path(name: str) -> str:
    if name not in ALLOWED_FILES:
        raise ValueError("不允许访问该配置文件")
    return os.path.join(CONFIG_DIR, name)


def read_config(name: str) -> Tuple[str, str]:
    """返回 (内容, 错误信息)。文件不存在时返回空内容而非报错。"""
    path = _path(name)
    try:
        if not os.path.exists(path):
            return "", ""
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read(), ""
    except Exception as exc:
        logger.warning("读取配置失败 %s: %s", name, exc)
        return "", f"读取失败: {exc}"


def _validate_txt(name: str, content: str) -> List[str]:
    """纯文本配置的行级校验，返回清洗后的行。"""
    lines = [ln.strip() for ln in content.replace("\r\n", "\n").split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return []

    if name == "summary_times.txt":
        import re
        for ln in lines:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", ln):
                raise ValueError(f"时间格式应为 HH:MM：{ln}")
    elif name == "delay_times.txt":
        for ln in lines:
            if not ln.isdigit() or int(ln) <= 0:
                raise ValueError(f"延时应为正整数：{ln}")
    elif name == "max_media_size.txt":
        for ln in lines:
            try:
                float(ln)
            except ValueError:
                raise ValueError(f"大小应为数字：{ln}")
    elif name == "media_extensions.txt":
        for ln in lines:
            if ln.startswith(".") or "/" in ln or " " in ln:
                raise ValueError(f"扩展名不应带点、斜杠或空格：{ln}")
    return lines


def write_config(name: str, content: str) -> Dict[str, Any]:
    """写入配置。校验不通过抛 ValueError，成功返回统计信息。"""
    path = _path(name)
    os.makedirs(CONFIG_DIR, exist_ok=True)

    if name.endswith(".json"):
        try:
            data = json.loads(content or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 格式错误: {exc.msg}（第 {exc.lineno} 行）")
        if not isinstance(data, dict):
            raise ValueError("顶层必须是对象：{provider: [模型, ...]}")
        cleaned: Dict[str, List[str]] = {}
        for provider, models in data.items():
            if not isinstance(provider, str):
                raise ValueError("provider 名称必须是字符串")
            if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
                raise ValueError(f"{provider} 的值必须是模型名数组")
            cleaned[provider.strip()] = [m.strip() for m in models if m.strip()]
        payload = json.dumps(cleaned, ensure_ascii=False, indent=4)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(payload)
        return {"name": name, "lines": len(cleaned), "written": len(payload)}

    lines = _validate_txt(name, content)
    payload = "\n".join(lines) + ("\n" if lines else "")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload)
    return {"name": name, "lines": len(lines), "written": len(payload)}


def list_configs() -> List[Dict[str, str]]:
    """列出可编辑配置及其当前内容。"""
    out = []
    for name, label in ALLOWED_FILES.items():
        content, error = read_config(name)
        out.append({
            "name": name,
            "label": label,
            "content": content,
            "error": error,
            "exists": os.path.exists(os.path.join(CONFIG_DIR, name)),
        })
    return out
