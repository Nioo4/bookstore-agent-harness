from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.memory.errors import MemoryKeyUnsupported, MemorySensitiveContent, MemoryValueInvalid


class MemoryType(StrEnum):
    PREFERENCE = "PREFERENCE"
    NEGATIVE_PREFERENCE = "NEGATIVE_PREFERENCE"
    EPISODE = "EPISODE"


class MemorySource(StrEnum):
    EXPLICIT = "EXPLICIT"
    INFERRED = "INFERRED"


class MemoryOperation(StrEnum):
    SET = "SET"
    ADD = "ADD"
    REMOVE = "REMOVE"


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_type: MemoryType
    memory_key: str = Field(min_length=1, max_length=191, description="Supported keys: preference.languages/topics/authors, negative.languages/topics/authors, preference.budget.CURRENCY (ISO 4217), or episode.AUTO for extraction.")
    operation: MemoryOperation
    value: dict[str, Any] = Field(description='For language/topic/author collections use {"items":["value"]}; language items are BCP 47 codes (zh-CN for Chinese, en for English). Budget uses {"currency":"CNY","max_amount":5000} in minor units. EPISODE uses {"summary":"durable reading summary","book_ids":[1]}. Do not add other fields.')
    source: MemorySource
    confidence: float = Field(ge=0, le=1)
    evidence_excerpt: str | None = Field(default=None, max_length=512)


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    memory_id: str
    memory_type: MemoryType
    memory_key: str
    value: dict[str, Any]
    source: MemorySource
    confidence: float
    created_at: datetime
    updated_at: datetime


class ApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory: MemoryRecord | None = None
    applied: bool
    replayed: bool = False
    ignored_reason: str | None = None


COLLECTION_KEYS: dict[str, MemoryType] = {
    "preference.languages": MemoryType.PREFERENCE,
    "preference.topics": MemoryType.PREFERENCE,
    "preference.authors": MemoryType.PREFERENCE,
    "negative.languages": MemoryType.NEGATIVE_PREFERENCE,
    "negative.topics": MemoryType.NEGATIVE_PREFERENCE,
    "negative.authors": MemoryType.NEGATIVE_PREFERENCE,
}
BUDGET_KEY = re.compile(r"^preference\.budget\.([A-Z]{3})$")
EPISODE_KEY = re.compile(r"^episode\.([0-9a-f]{64})$")
LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
V2_LANGUAGE_ALIASES = {
    "中文": "zh-CN",
    "简体中文": "zh-CN",
    "繁体中文": "zh-TW",
    "英文": "en",
    "英语": "en",
}

_EMAIL = re.compile(r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{7,}\d)(?!\d)")
_PAYMENT = re.compile(r"(?i)(?:card|cvv|银行卡|信用卡|支付账号|账户号).{0,24}\d")
_SECRET = re.compile(r"(?i)(?:password|passwd|pwd|jwt|access[_ -]?token|verification[_ -]?token|密码|验证码|令牌)\s*[:=：]?\s*\S+")
_ADDRESS = re.compile(r"(?i)(?:完整地址|收货地址|shipping address|home address)\s*[:=：]?\s*\S+")
_REALTIME_FACT = re.compile(
    r"(?i)(?:库存|stock|价格|price|订单状态|order status).{0,20}(?:\d|有货|无货|in stock|out of stock|placed|cancelled)"
)


def normalize_candidate(
    candidate: MemoryCandidate,
    *,
    source_message_id: str,
    inferred_min_confidence: float,
) -> MemoryCandidate:
    key = unicodedata.normalize("NFKC", candidate.memory_key).strip()
    if candidate.source is MemorySource.EXPLICIT:
        confidence = 1.0
    else:
        confidence = candidate.confidence
        if confidence < inferred_min_confidence:
            raise MemoryValueInvalid("inferred memory confidence is below the configured threshold")

    if key in COLLECTION_KEYS:
        expected_type = COLLECTION_KEYS[key]
        if candidate.memory_type is not expected_type:
            raise MemoryKeyUnsupported("memory key does not match memory type")
        if candidate.operation not in {MemoryOperation.SET, MemoryOperation.ADD, MemoryOperation.REMOVE}:
            raise MemoryValueInvalid("collection memory only supports SET, ADD, and REMOVE")
        language_values = key.endswith(".languages")
        value = {"items": _normalize_items(candidate.value, languages=language_values)}
    elif budget := BUDGET_KEY.fullmatch(key):
        if candidate.memory_type is not MemoryType.PREFERENCE or candidate.operation is not MemoryOperation.SET:
            raise MemoryValueInvalid("budget memory only supports PREFERENCE SET")
        currency = candidate.value.get("currency")
        amount = candidate.value.get("max_amount")
        if currency != budget.group(1) or isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise MemoryValueInvalid("budget value must match the key currency and use a nonnegative integer amount")
        if set(candidate.value) != {"currency", "max_amount"}:
            raise MemoryValueInvalid("budget value contains unsupported fields")
        value = {"currency": currency, "max_amount": amount}
    elif match := EPISODE_KEY.fullmatch(key):
        if candidate.memory_type is not MemoryType.EPISODE or candidate.operation is not MemoryOperation.SET:
            raise MemoryValueInvalid("episode memory only supports EPISODE SET")
        value = _normalize_episode(candidate.value)
        expected_hash = episode_content_hash(value, source_message_id)
        if match.group(1) != expected_hash:
            raise MemoryValueInvalid("episode key does not match normalized episode content")
    else:
        raise MemoryKeyUnsupported("memory key is not supported")

    evidence = _normalize_optional(candidate.evidence_excerpt)
    _reject_sensitive(value, evidence)
    return candidate.model_copy(
        update={"memory_key": key, "value": value, "confidence": confidence, "evidence_excerpt": evidence}
    )


def episode_content_hash(value: dict[str, Any], source_message_id: str) -> str:
    canonical = canonical_json(
        {
            "summary": value["summary"],
            "book_ids": sorted(value["book_ids"]),
            "source_message_id": unicodedata.normalize("NFKC", source_message_id).strip(),
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def episode_key_for(value: dict[str, Any], source_message_id: str) -> tuple[str, dict[str, Any]]:
    normalized = _normalize_episode(value)
    return f"episode.{episode_content_hash(normalized, source_message_id)}", normalized


def content_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def write_idempotency_key(
    user_id: int,
    conversation_id: str,
    message_id: str,
    candidate_index: int,
    candidate: MemoryCandidate,
) -> str:
    canonical = canonical_json(
        {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "candidate_index": candidate_index,
            "candidate": candidate.model_dump(mode="json"),
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_items(value: dict[str, Any], *, languages: bool) -> list[str]:
    if set(value) != {"items"} or not isinstance(value.get("items"), list):
        raise MemoryValueInvalid("collection value must contain only an items array")
    items: list[str] = []
    seen: set[str] = set()
    for raw in value["items"]:
        if not isinstance(raw, str):
            raise MemoryValueInvalid("memory items must be strings")
        item = unicodedata.normalize("NFKC", raw).strip()
        if not item:
            raise MemoryValueInvalid("memory items must not be empty")
        if languages:
            item = _normalize_language(item)
        folded = item.casefold()
        if folded not in seen:
            seen.add(folded)
            items.append(item)
    if not 1 <= len(items) <= 20:
        raise MemoryValueInvalid("memory items must contain between 1 and 20 unique values")
    return items


def _normalize_language(value: str) -> str:
    if not LANGUAGE_TAG.fullmatch(value):
        raise MemoryValueInvalid("language memory must use a valid BCP 47 tag")
    parts = value.split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif len(part) == 2 and part.isalpha() or len(part) == 3 and part.isdigit():
            normalized.append(part.upper())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


def normalize_v2_preference_value(key: str, value: str) -> str:
    """Normalize the public v2 preference contract before persistence."""
    normalized = unicodedata.normalize("NFKC", value).strip()
    if key == "PREFERRED_LANGUAGE":
        return _normalize_language(V2_LANGUAGE_ALIASES.get(normalized, normalized))
    if key == "MAX_USUAL_BUDGET":
        try:
            amount = int(normalized)
        except ValueError as exc:
            raise MemoryValueInvalid("budget preference must be a nonnegative integer in minor units") from exc
        if amount < 0 or str(amount) != normalized:
            raise MemoryValueInvalid("budget preference must be a nonnegative integer in minor units")
        return str(amount)
    return normalized


def _normalize_episode(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {"summary", "book_ids"}:
        raise MemoryValueInvalid("episode value must contain summary and book_ids")
    summary = value.get("summary")
    book_ids = value.get("book_ids")
    if not isinstance(summary, str) or not isinstance(book_ids, list):
        raise MemoryValueInvalid("episode summary and book_ids have invalid types")
    summary = unicodedata.normalize("NFKC", summary).strip()
    if not 1 <= len(summary) <= 500:
        raise MemoryValueInvalid("episode summary must contain between 1 and 500 characters")
    normalized_ids: list[int] = []
    for book_id in book_ids:
        if isinstance(book_id, bool) or not isinstance(book_id, int) or book_id <= 0:
            raise MemoryValueInvalid("episode book_ids must be positive integers")
        if book_id not in normalized_ids:
            normalized_ids.append(book_id)
    return {"summary": summary, "book_ids": sorted(normalized_ids)}


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    return normalized[:512] or None


def _reject_sensitive(value: dict[str, Any], evidence: str | None) -> None:
    string_values: list[str] = []
    for item in value.values():
        if isinstance(item, str):
            string_values.append(item)
        elif isinstance(item, list):
            string_values.extend(part for part in item if isinstance(part, str))
    if evidence:
        string_values.append(evidence)
    text = "\n".join(string_values)
    if any(pattern.search(text) for pattern in (_EMAIL, _JWT, _PHONE, _PAYMENT, _SECRET, _ADDRESS, _REALTIME_FACT)):
        raise MemorySensitiveContent("memory candidate contains prohibited sensitive or real-time content")
