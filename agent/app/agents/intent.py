"""Deterministic request schema; money stays in catalog minor units."""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from app.agents.verification import UserConstraints


INTENT_PROMPT = (
    "Extract only explicit questions and constraints. Do not infer constraints. Return JSON only. "
    "All money fields are integer minor units: CNY 50元 = 5000分, 100元 = 10000分, 50分 = 50. "
    "max_price is a per-book cap; total_budget is the combined budget only when explicitly requested. "
    "Language: Chinese=zh-CN, English=en. A reading preference is not a system policy question. "
    "Do not return stock-alert fields: explicit alert requests are parsed separately by the service."
)


class InventoryWatchIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book_identifier: str | None = Field(default=None, min_length=1, max_length=255)
    desired_quantity: int = Field(default=1, ge=1, le=99)
    duration_seconds: int | None = Field(default=None, ge=3600, le=90 * 24 * 60 * 60)


class UserIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_question_ids: list[str] = Field(default_factory=list)
    constraints: UserConstraints = Field(default_factory=UserConstraints)


_WATCH_REQUEST = re.compile(
    r"(?:(?:补货|补到|到货|库存).{0,24}?(?:监听|提醒|通知)|(?:监听|提醒|通知).{0,24}?(?:补货|补到|到货|库存)|\bwatch\b|\balert\b)",
    re.IGNORECASE,
)
_NEGATED_WATCH = re.compile(r"(?:(?:不要|不需要|无需|取消).{0,8}?(?:监听|提醒|通知)|\b(?:don't|do not|no)\s+(?:watch|alert)\b)", re.IGNORECASE)
_ISBN = re.compile(r"\b(?:ISBN\s*[:：]?\s*)?([0-9][0-9Xx-]{8,16}[0-9Xx])\b", re.IGNORECASE)
_BOOK_ID = re.compile(r"(?:\bB|\bbook\s+|#)(\d{1,8})\b", re.IGNORECASE)
_TITLE = re.compile(r"《([^》]{1,120})》")
_WATCH_QUANTITY = re.compile(r"(?:(?:库存|补货|补到|到货).{0,16}?(?:达到|到)?\s*([\d一二两三四五六七八九]{1,2})\s*(?:本|册|部)|\bat\s+(\d{1,2})\s+(?:copies|books?)\b)", re.IGNORECASE)
_WATCH_DURATION = re.compile(r"(\d{1,3})\s*(天|日|小时|时|hours?|hrs?)", re.IGNORECASE)
_CHINESE_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def infer_inventory_watch(content: str) -> InventoryWatchIntent | None:
    """Parse only an explicit watch request outside the versioned LLM schema.

    Keeping `UserIntent` stable preserves existing v2 scripted response hashes;
    this deterministic parser deliberately does not infer a target when the user
    did not provide one or the current page cannot resolve a reference.
    """
    # A catalog title can itself contain “到货通知”; querying that title is
    # not an instruction to create a watch. Match the action outside quoted
    # titles, then still use the original content to resolve the identifier.
    action_content = _TITLE.sub("", content)
    if not _WATCH_REQUEST.search(action_content) or _NEGATED_WATCH.search(action_content):
        return None
    identifier = None
    if match := _ISBN.search(content):
        identifier = match.group(1).replace("-", "")
    elif match := _BOOK_ID.search(content):
        identifier = match.group(1)
    elif match := _TITLE.search(content):
        identifier = match.group(1).strip()

    quantity_match = _WATCH_QUANTITY.search(content)
    quantity = next(group for group in quantity_match.groups() if group is not None) if quantity_match else None
    desired_quantity = (
        _CHINESE_DIGITS[quantity] if quantity in _CHINESE_DIGITS else int(quantity)
    ) if quantity is not None else 1
    duration_seconds = None
    if match := _WATCH_DURATION.search(content):
        amount, unit = int(match.group(1)), match.group(2)
        duration_seconds = amount * (24 * 60 * 60 if unit.casefold() in {"天", "日"} else 60 * 60)
    return InventoryWatchIntent(
        book_identifier=identifier,
        desired_quantity=desired_quantity,
        duration_seconds=duration_seconds,
    )


def watch_books(books: dict[int, dict], intent: InventoryWatchIntent) -> list[dict]:
    """Choose only a uniquely identified, real Catalog result. Never infer IDs."""
    identifier = intent.book_identifier.strip().casefold() if intent.book_identifier else None
    matches = [book for book in books.values() if identifier is None or identifier in {
        str(book.get(key, "")).strip().casefold() for key in ("book_id", "isbn", "title")
    }]
    if len(matches) != 1:
        return []
    book = matches[0]
    return [book] if book.get("status", "ACTIVE") == "ACTIVE" and book["available_stock"] < intent.desired_quantity else []
