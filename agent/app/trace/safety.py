from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.trace.types import canonical_json


_DENIED_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "confirmation_token",
    "cookie",
    "email",
    "evidence_excerpt",
    "full_address",
    "idempotency_key",
    "ip",
    "jwt",
    "memory_evidence",
    "memory_value",
    "model_api_key",
    "password",
    "payment_info",
    "phone",
    "refresh_token",
    "smtp_password",
    "system_prompt",
    "developer_prompt",
    "token",
    "user_agent",
}

_PATTERNS = (
    re.compile(r"(?i)(?:\b(?:password|passwd|pwd|(?:access[_ -]?|refresh[_ -]?|confirmation[_ -]?)?token|(?:model[_ -]?)?api[_ -]?key)|密码|验证码|令牌)\s*[:=：]\s*[^\s,;，；。]+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)(?:\+?[1-9]\d{0,2}[- .]?)?(?:\(?\d{2,4}\)?[- .]?)\d{6,10}(?!\d)"),
    re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),
    re.compile(r"(?i)\b(?:sk|pk|api)[-_][A-Za-z0-9_-]{12,}\b"),
    # A single Chinese “省/市/区” character inside a title (for example
    # “元数据缺省样本”) is not an address. Require either two geographic
    # segments, a street/community marker, or a numbered address.
    re.compile(
        r"(?:[\u4e00-\u9fff]{2,}(?:省|市|区|县)[\u4e00-\u9fff]{2,}(?:省|市|区|县)[\u4e00-\u9fff\dA-Za-z-]*|"
        r"[\u4e00-\u9fff]{2,}(?:街道|路|小区)[\u4e00-\u9fff\dA-Za-z-]{2,}|"
        r"[\u4e00-\u9fff]{2,}[0-9一二三四五六七八九十百]+号[\u4e00-\u9fff\dA-Za-z-]*)"
    ),
)

_ISBN_REFERENCE = re.compile(r"(?i)\bISBN(?:-1[03])?\s*[:：]?\s*([0-9][0-9 -]{8,24}[0-9X])(?![0-9X])")


def _valid_isbn(value: str) -> bool:
    digits = re.sub(r"[ -]", "", value).upper()
    if len(digits) == 13 and digits.isascii() and digits.isdigit() and digits.startswith(("978", "979")):
        return sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(digits)) % 10 == 0
    if len(digits) == 10 and digits[:9].isascii() and digits[:9].isdigit() and digits[-1] in "0123456789X":
        return sum((10 - index) * (10 if digit == "X" else int(digit)) for index, digit in enumerate(digits)) % 11 == 0
    return False


@dataclass(frozen=True)
class SafeProjection:
    value: Any
    changed: bool
    truncated: bool
    original_bytes: int
    content_hash: str

    @property
    def replay_safe(self) -> bool:
        return not self.changed and not self.truncated


class TraceSafetyProjector:
    def __init__(
        self,
        *,
        max_json_bytes: int = 65_536,
        max_final_answer_bytes: int = 32_768,
        max_error_bytes: int = 2_048,
        max_evidence_bytes: int = 8_192,
    ) -> None:
        self.max_json_bytes = max_json_bytes
        self.max_final_answer_bytes = max_final_answer_bytes
        self.max_error_bytes = max_error_bytes
        self.max_evidence_bytes = max_evidence_bytes

    def json(self, value: Any, *, limit: int | None = None) -> SafeProjection:
        safe, changed = self._walk(value)
        encoded = canonical_json(safe).encode("utf-8")
        original_bytes = len(encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        maximum = limit if limit is not None else self.max_json_bytes
        if original_bytes <= maximum:
            return SafeProjection(safe, changed, False, original_bytes, digest)
        return SafeProjection(
            {
                "truncated": True,
                "original_bytes": original_bytes,
                "content_hash": digest,
            },
            True,
            True,
            original_bytes,
            digest,
        )

    def evidence(self, value: Any) -> SafeProjection:
        return self.json(value, limit=self.max_evidence_bytes)

    def text(self, value: str, *, kind: str = "final_answer") -> SafeProjection:
        safe, changed = self._redact_string(value)
        encoded = safe.encode("utf-8")
        original_bytes = len(encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        maximum = self.max_final_answer_bytes if kind == "final_answer" else self.max_error_bytes
        if original_bytes <= maximum:
            return SafeProjection(safe, changed, False, original_bytes, digest)
        marker = f"[TRUNCATED original_bytes={original_bytes} content_hash={digest}]"
        return SafeProjection(marker, True, True, original_bytes, digest)

    def _walk(self, value: Any) -> tuple[Any, bool]:
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            changed = False
            for key, item in value.items():
                name = str(key)
                if name.casefold() in _DENIED_KEYS or self._looks_secret_key(name):
                    output[name] = "[REDACTED]"
                    changed = True
                    continue
                if name.casefold() == "isbn" and isinstance(item, str):
                    output[name], nested_changed = self._redact_string(item, isbn_field=True)
                    changed = changed or nested_changed
                    continue
                projected, nested_changed = self._walk(item)
                output[name] = projected
                changed = changed or nested_changed
            return output, changed
        if isinstance(value, (list, tuple)):
            values: list[Any] = []
            changed = False
            for item in value:
                projected, nested_changed = self._walk(item)
                values.append(projected)
                changed = changed or nested_changed
            return values, changed
        if isinstance(value, str):
            return self._redact_string(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value, False
        normalized = json.loads(json.dumps(value, default=str, ensure_ascii=False))
        projected, _ = self._walk(normalized)
        return projected, True

    @staticmethod
    def _looks_secret_key(name: str) -> bool:
        folded = name.casefold().replace("-", "_")
        return folded.endswith(("_password", "_secret", "_token", "_api_key"))

    @staticmethod
    def _redact_string(value: str, *, isbn_field: bool = False) -> tuple[str, bool]:
        result = value
        changed = False
        for pattern in _PATTERNS:
            if pattern in _PATTERNS[4:6]:
                # Only phone matching is exempted, and only for a checksummed
                # ISBN in an ISBN field or explicitly labelled ISBN reference.
                spans = [(match.start(1), match.end(1)) for match in _ISBN_REFERENCE.finditer(result)
                         if _valid_isbn(match.group(1))]
                if isbn_field and _valid_isbn(result):
                    spans.append((0, len(result)))
                replaced = pattern.sub(
                    lambda match: match.group() if any(start <= match.start() and match.end() <= end for start, end in spans)
                    else "[REDACTED]", result,
                )
                count = int(replaced != result)
            else:
                replaced, count = pattern.subn("[REDACTED]", result)
            result = replaced
            changed = changed or count > 0
        return result, changed
