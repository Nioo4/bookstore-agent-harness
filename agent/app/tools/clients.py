from __future__ import annotations

from typing import Any, Protocol

import grpc
from app.telemetry import rpc_metadata, traced

from app.tools.errors import ToolError
from app.tools.schemas import (
    CreateOrderInput,
    GetBookInput,
    GetOrderInput,
    ListMyOrdersInput,
    PrepareOrderInput,
    PrepareOrderRequest,
    SearchBooksInput,
)


class CatalogClient(Protocol):
    async def search_books(self, request: SearchBooksInput, timeout: float) -> dict[str, Any]: ...

    async def get_book(self, request: GetBookInput, timeout: float) -> dict[str, Any]: ...


class OrderClient(Protocol):
    async def prepare_order(self, request: PrepareOrderRequest, access_token: str, conversation_id: str, timeout: float) -> dict[str, Any]: ...

    async def create_order(self, request: CreateOrderInput, access_token: str, conversation_id: str, timeout: float) -> dict[str, Any]: ...

    async def get_order(self, request: GetOrderInput, access_token: str, timeout: float) -> dict[str, Any]: ...

    async def list_my_orders(self, request: ListMyOrdersInput, access_token: str, timeout: float) -> dict[str, Any]: ...


def _book_summary(message: Any) -> dict[str, Any]:
    result = {
        "book_id": message.book_id,
        "isbn": message.isbn,
        "title": message.title,
        "subtitle": message.subtitle,
        "description": message.description,
        "publisher": message.publisher,
        "publication_date": message.publication_date,
        "language_code": message.language_code,
        "cover_url": message.cover_url,
        "price": message.price,
        "currency": message.currency,
        "status": message.status,
        "version": message.version,
        "available_stock": message.available_stock,
        "authors": list(message.authors),
        "categories": list(message.categories),
    }
    if message.HasField("page_count"):
        result["page_count"] = message.page_count
    return result


def _order_item(message: Any) -> dict[str, Any]:
    return {
        "book_id": message.book_id,
        "isbn": message.isbn,
        "title": message.title,
        "unit_price": message.unit_price,
        "quantity": message.quantity,
        "line_total": message.line_total,
    }


def _order_record(message: Any) -> dict[str, Any]:
    return {
        "order_id": message.order_id,
        "status": message.status,
        "items": [_order_item(item) for item in message.items],
        "total_amount": message.total_amount,
        "currency": message.currency,
        "created_at": message.created_at,
        "replayed": message.replayed,
    }


def _confirmation_result(message: Any) -> dict[str, Any]:
    return {
        "confirmation_id": message.confirmation_id,
        "decision": message.decision,
        "status": message.status,
        "order": _order_record(message.order) if message.HasField("order") else None,
        "replayed": message.replayed,
    }


def _rpc_error(exc: grpc.aio.AioRpcError) -> ToolError:
    detail = exc.details() or "business RPC failed"
    normalized = detail.casefold().replace("_", " ")
    phrases = {
        "book not found": "BOOK_NOT_FOUND",
        "order not found": "ORDER_NOT_FOUND",
        "email must be verified": "EMAIL_NOT_VERIFIED",
        "book is inactive": "BOOK_INACTIVE",
        "inventory insufficient": "INVENTORY_INSUFFICIENT",
        "confirmation expired": "CONFIRMATION_EXPIRED",
        "confirmation already used": "CONFIRMATION_ALREADY_USED",
        "confirmation already decided": "CONFIRMATION_ALREADY_DECIDED",
        "confirmation not found": "CONFIRMATION_NOT_FOUND",
        "price changed": "PRICE_CHANGED",
        "idempotency key conflict": "IDEMPOTENCY_CONFLICT",
    }
    code = next((mapped for phrase, mapped in phrases.items() if phrase in normalized), "BUSINESS_RPC_ERROR")
    retryable = exc.code() in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}
    return ToolError(code, detail, retryable=retryable)


class GrpcCatalogClient:
    def __init__(self, address: str) -> None:
        self._address = address
        self._channel = None
        self._stub = None

    def _get_stub(self):
        from rpc.check import check_pb2_grpc

        if self._stub is None:
            self._channel = grpc.aio.insecure_channel(self._address)
            self._stub = check_pb2_grpc.checkerStub(self._channel)
        return self._stub

    @traced("rpc.catalog.search")
    async def search_books(self, request: SearchBooksInput, timeout: float) -> dict[str, Any]:
        from rpc.check import check_pb2

        kwargs = request.model_dump(exclude_none=True)
        try:
            response = await self._get_stub().searchBooks(check_pb2.searchBooksReq(**kwargs), timeout=timeout, metadata=rpc_metadata())
            return {"books": [_book_summary(book) for book in response.books], "next_cursor": response.next_cursor}
        except grpc.aio.AioRpcError as exc:
            raise _rpc_error(exc) from exc

    @traced("rpc.catalog.get")
    async def get_book(self, request: GetBookInput, timeout: float) -> dict[str, Any]:
        from rpc.check import check_pb2

        identifier = str(request.book_id) if request.book_id is not None else request.isbn
        try:
            response = await self._get_stub().check(check_pb2.checkReq(book=identifier), timeout=timeout, metadata=rpc_metadata())
            if not response.found:
                raise ToolError("BOOK_NOT_FOUND", "book not found")
            result = {
                "book_id": response.book_id,
                "isbn": response.isbn,
                "title": response.title,
                "subtitle": response.subtitle,
                "description": response.description,
                "publisher": response.publisher,
                "publication_date": response.publication_date,
                "language_code": response.language_code,
                "cover_url": response.cover_url,
                "price": response.price,
                "currency": response.currency,
                "status": response.status,
                "version": response.version,
                "available_stock": response.stock_count,
                "authors": list(response.authors),
                "categories": list(response.categories),
            }
            if response.HasField("page_count"):
                result["page_count"] = response.page_count
            return result
        except grpc.aio.AioRpcError as exc:
            raise _rpc_error(exc) from exc

    async def aclose(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel, self._stub = None, None


class GrpcOrderClient:
    def __init__(self, address: str) -> None:
        self._address = address
        self._channel = None
        self._stub = None

    def _get_stub(self):
        from rpc.order import bookstore_order_pb2_grpc

        if self._stub is None:
            self._channel = grpc.aio.insecure_channel(self._address)
            self._stub = bookstore_order_pb2_grpc.orderServiceStub(self._channel)
        return self._stub

    @traced("rpc.order.prepare")
    async def prepare_order(self, request: PrepareOrderRequest, access_token: str, conversation_id: str, timeout: float) -> dict[str, Any]:
        from rpc.order import bookstore_order_pb2

        message = bookstore_order_pb2.prepareOrderReq(
            access_token=access_token,
            conversation_id=conversation_id,
            idempotency_key=request.idempotency_key,
            items=[bookstore_order_pb2.orderItemInput(**item.model_dump()) for item in request.items],
        )
        try:
            response = await self._get_stub().prepareOrder(message, timeout=timeout, metadata=rpc_metadata())
            return {
                "confirmation_token": response.confirmation_token,
                "expires_at": response.expires_at,
                "items": [_order_item(item) for item in response.items],
                "total_amount": response.total_amount,
                "currency": response.currency,
            }
        except grpc.aio.AioRpcError as exc:
            raise _rpc_error(exc) from exc

    @traced("rpc.order.decide")
    async def create_order(self, request: CreateOrderInput, access_token: str, conversation_id: str, timeout: float) -> dict[str, Any]:
        from rpc.order import bookstore_order_pb2

        try:
            response = await self._get_stub().decideConfirmation(
                bookstore_order_pb2.decideConfirmationReq(
                    access_token=access_token,
                    confirmation_token=request.confirmation_token,
                    conversation_id=conversation_id,
                    decision=request.decision,
                ),
                timeout=timeout, metadata=rpc_metadata(),
            )
            return _confirmation_result(response)
        except grpc.aio.AioRpcError as exc:
            raise _rpc_error(exc) from exc

    @traced("rpc.order.get")
    async def get_order(self, request: GetOrderInput, access_token: str, timeout: float) -> dict[str, Any]:
        from rpc.order import bookstore_order_pb2

        try:
            response = await self._get_stub().getOrder(
                bookstore_order_pb2.getOrderReq(access_token=access_token, order_id=request.order_id), timeout=timeout, metadata=rpc_metadata()
            )
            return _order_record(response)
        except grpc.aio.AioRpcError as exc:
            raise _rpc_error(exc) from exc

    @traced("rpc.order.list")
    async def list_my_orders(self, request: ListMyOrdersInput, access_token: str, timeout: float) -> dict[str, Any]:
        from rpc.order import bookstore_order_pb2

        try:
            response = await self._get_stub().listMyOrders(
                bookstore_order_pb2.listMyOrdersReq(access_token=access_token, limit=request.limit, cursor=request.cursor),
                timeout=timeout, metadata=rpc_metadata(),
            )
            return {"orders": [_order_record(order) for order in response.orders], "next_cursor": response.next_cursor}
        except grpc.aio.AioRpcError as exc:
            raise _rpc_error(exc) from exc

    async def aclose(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel, self._stub = None, None
