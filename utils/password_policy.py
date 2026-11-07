"""密码强度策略。

统一约束：至少 8 位，且同时包含大写字母、小写字母与数字。

放在独立模块而不是路由里，是因为注册、改密码、以及将来的重置入口
都要复用同一套规则——规则散落各处是这类校验最容易走样的地方。
"""
from __future__ import annotations

import re
from typing import Tuple

MIN_LENGTH = 8

_LOWER = re.compile(r"[a-z]")
_UPPER = re.compile(r"[A-Z]")
_DIGIT = re.compile(r"\d")


def describe() -> str:
    """给界面提示用的规则说明。"""
    return "至少 8 位，且同时包含大写字母、小写字母和数字"


def validate_password(password: str) -> Tuple[bool, str]:
    """校验密码强度，返回 (是否通过, 错误说明)。"""
    text = password or ""

    if len(text) < MIN_LENGTH:
        return False, f"密码至少需要 {MIN_LENGTH} 位"
    if not _LOWER.search(text):
        return False, "密码需要包含小写字母"
    if not _UPPER.search(text):
        return False, "密码需要包含大写字母"
    if not _DIGIT.search(text):
        return False, "密码需要包含数字"
    return True, ""
