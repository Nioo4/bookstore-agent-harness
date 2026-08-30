from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SearchBooksInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = ""
    author: str = ""
    category: str = ""
    min_price: int | None = Field(default=None, ge=0)
    max_price: int | None = Field(default=None, ge=0)
    language: str = ""
    in_stock: bool | None = None
    sort: Literal["relevance", "price_asc", "price_desc", "newest"] = "relevance"
    cursor: str = ""
    limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def validate_prices(self) -> "SearchBooksInput":
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise ValueError("min_price must not exceed max_price")
        return self


class GetBookInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book_id: int | None = Field(default=None, gt=0)
    isbn: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def exactly_one_identifier(self) -> "GetBookInput":
        if (self.book_id is None) == (self.isbn is None):
            raise ValueError("exactly one of book_id or isbn is required")
        return self


class SearchPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=1000)
    limit: int = Field(default=5, ge=1, le=5)


class OrderItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=99)


class PrepareOrderInput(BaseModel):
    # Identity-like model fields are discarded; authenticated context is authoritative.
    model_config = ConfigDict(extra="ignore")

    items: list[OrderItemInput] = Field(min_length=1, max_length=50)
    idempotency_key: str = Field(min_length=8, max_length=128)


class CreateOrderInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    confirmation_token: str = Field(min_length=32, max_length=512)
    decision: Literal["APPROVE", "REJECT"]


class GetOrderInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_id: int = Field(gt=0)


class ListMyOrdersInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    limit: int = Field(default=20, ge=1, le=50)
    cursor: str = ""
