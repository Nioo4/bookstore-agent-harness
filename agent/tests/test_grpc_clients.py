from __future__ import annotations

from typing import Any
import asyncio
import grpc

import pytest

from app.tools.clients import GrpcCatalogClient, GrpcOrderClient
from app.tools.schemas import CreateOrderInput, PrepareOrderRequest, PrepareOrderInput, SearchBooksInput
from rpc.check import check_pb2
from rpc.check import check_pb2_grpc
from rpc.order import bookstore_order_pb2


class CatalogStub:
    def __init__(self) -> None:
        self.request: Any = None
        self.timeout: float | None = None

    async def searchBooks(self, request: Any, timeout: float, metadata=()):
        self.metadata = metadata
        self.request = request
        self.timeout = timeout
        return check_pb2.searchBooksResp(
            books=[
                check_pb2.bookSummary(
                    book_id=7,
                    isbn="9780000000007",
                    title="Agent Systems",
                    price=8800,
                    currency="CNY",
                    status="ACTIVE",
                    version=4,
                    available_stock=5,
                    authors=["A. Author"],
                    categories=["Technology"],
                )
            ],
            next_cursor="next",
        )


class OrderStub:
    def __init__(self) -> None:
        self.request: Any = None

    async def prepareOrder(self, request: Any, timeout: float, metadata=()):
        self.metadata = metadata
        self.request = request
        return bookstore_order_pb2.prepareOrderResp(
            confirmation_token="raw-confirmation",
            expires_at="2026-08-26T00:10:00Z",
            items=[
                bookstore_order_pb2.orderItem(
                    book_id=7,
                    isbn="9780000000007",
                    title="Agent Systems",
                    unit_price=8800,
                    quantity=2,
                    line_total=17600,
                )
            ],
            total_amount=17600,
            currency="CNY",
        )

    async def decideConfirmation(self, request: Any, timeout: float, metadata=()):
        self.metadata = metadata
        self.request = request
        return bookstore_order_pb2.confirmationResult(
            confirmation_id="action-1",
            decision=request.decision,
            status="COMPLETED",
            order=bookstore_order_pb2.orderRecord(
                order_id=91,
                status="PLACED",
                total_amount=17600,
                currency="CNY",
            ),
            replayed=False,
        )


@pytest.mark.asyncio
async def test_catalog_grpc_client_preserves_filters_and_evidence_fields() -> None:
    stub = CatalogStub()
    client = GrpcCatalogClient.__new__(GrpcCatalogClient)
    client._stub = stub  # type: ignore[attr-defined]

    result = await client.search_books(
        SearchBooksInput(q="agent", category="Technology", in_stock=True, limit=10), timeout=3
    )

    assert stub.request.q == "agent"
    assert stub.request.category == "Technology"
    assert stub.request.in_stock is True
    assert stub.timeout == 3
    assert result["books"][0]["version"] == 4
    assert result["books"][0]["available_stock"] == 5


@pytest.mark.asyncio
async def test_order_grpc_client_uses_context_token_and_conversation() -> None:
    stub = OrderStub()
    client = GrpcOrderClient.__new__(GrpcOrderClient)
    client._stub = stub  # type: ignore[attr-defined]
    request = PrepareOrderRequest(items=[{"book_id": 7, "quantity": 2}], idempotency_key="idem-key-123")

    result = await client.prepare_order(request, "verified-jwt", "conversation-7", timeout=5)

    assert stub.request.access_token == "verified-jwt"
    assert stub.request.conversation_id == "conversation-7"
    assert not hasattr(stub.request, "user_id")
    assert result["total_amount"] == 17600
    assert result["items"][0]["quantity"] == 2


@pytest.mark.asyncio
async def test_order_grpc_confirmation_binds_jwt_conversation_and_structured_decision() -> None:
    stub = OrderStub()
    client = GrpcOrderClient.__new__(GrpcOrderClient)
    client._stub = stub  # type: ignore[attr-defined]

    result = await client.create_order(
        CreateOrderInput(confirmation_token="x" * 43, decision="APPROVE"),
        "verified-jwt",
        "conversation-7",
        timeout=8,
    )

    assert stub.request.access_token == "verified-jwt"
    assert stub.request.conversation_id == "conversation-7"
    assert stub.request.decision == "APPROVE"
    assert not hasattr(stub.request, "user_id")
    assert result["confirmation_id"] == "action-1"
    assert result["order"]["order_id"] == 91


def test_client_created_before_event_loop_uses_runtime_loop():
    # Uvicorn/console entry points may import app.main before choosing their loop.
    client = GrpcCatalogClient("unused")
    assert client._channel is None

    class Server(check_pb2_grpc.checkerServicer):
        async def searchBooks(self, request, context):
            return check_pb2.searchBooksResp(books=[check_pb2.bookSummary(book_id=12)])

    async def run():
        server = grpc.aio.server()
        check_pb2_grpc.add_checkerServicer_to_server(Server(), server)
        port = server.add_insecure_port("127.0.0.1:0")
        client._address = f"127.0.0.1:{port}"
        await server.start()
        try:
            result = await client.search_books(SearchBooksInput(), timeout=2)
            assert result["books"][0]["book_id"] == 12
        finally:
            await client.aclose()
            await server.stop(None)
    asyncio.run(run())
    assert client._channel is None
