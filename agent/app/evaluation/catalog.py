"""The approved synthetic directory; never a replacement for Catalog integration tests."""
from __future__ import annotations

from copy import deepcopy

from app.tools.errors import ToolError


BOOKS = {
    number: dict(book_id=number, isbn=isbn, title=title, authors=authors, categories=categories,
        language_code=language, price=price, available_stock=stock, status=status,
        currency="CNY", version=1, description="Synthetic acceptance catalog")
    for number, isbn, title, authors, categories, language, price, stock, status in [
        (1,"9780000000019","分布式入门",["林甲"],["distributed"],"zh-CN",3000,5,"ACTIVE"),
        (2,"9780000000026","分布式实践",["周乙"],["distributed"],"zh-CN",4000,5,"ACTIVE"),
        (3,"9780000000033","分布式进阶",["林甲"],["distributed"],"zh-CN",5000,5,"ACTIVE"),
        (4,"9780000000040","Distributed Systems",["Ann Test"],["distributed"],"en",3500,5,"ACTIVE"),
        (5,"9780000000057","分布式缺货本",["林甲"],["distributed"],"zh-CN",2000,0,"ACTIVE"),
        (6,"9780000000064","分布式旧版",["林甲"],["distributed"],"zh-CN",1000,5,"INACTIVE"),
        (7,"9780000000071","分布式昂贵本",["林甲"],["distributed"],"zh-CN",12000,5,"ACTIVE"),
        (8,"9780000000088","Java练习",["周乙"],["java"],"zh-CN",2500,5,"ACTIVE"),
        (9,"9780000000095","分布式入门",["周乙","林甲"],["distributed","database"],"zh-CN",4500,5,"ACTIVE"),
    ]
}


class ControlledCatalog:
    def __init__(self, books=None, *, fail=False) -> None:
        self.books = deepcopy(books if books is not None else BOOKS)
        self.calls = []
        self.fail = fail

    async def search_books(self, request, timeout):
        self.calls.append(("search_books", request.model_dump(mode="json")))
        if self.fail:
            raise TimeoutError("synthetic catalog timeout")
        rows = [book for book in self.books.values() if book["status"] == "ACTIVE"]
        if request.q:
            rows = [book for book in rows if request.q.casefold() in (book["title"]+" "+book["isbn"]).casefold()]
        if request.author: rows = [book for book in rows if request.author in book["authors"]]
        if request.category: rows = [book for book in rows if request.category in book["categories"]]
        if request.language: rows = [book for book in rows if request.language.casefold() == book["language_code"].casefold()]
        if request.min_price is not None: rows = [book for book in rows if book["price"] >= request.min_price]
        if request.max_price is not None: rows = [book for book in rows if book["price"] <= request.max_price]
        if request.in_stock is True: rows = [book for book in rows if book["available_stock"] > 0]
        if request.in_stock is False: rows = [book for book in rows if book["available_stock"] == 0]
        if request.sort.startswith("price_"):
            rows.sort(key=lambda book: (book["price"], book["book_id"]), reverse=request.sort == "price_desc")
        else:
            rows.sort(key=lambda book: book["book_id"])
        return {"books": deepcopy(rows[:request.limit]), "next_cursor":""}

    async def get_book(self, request, timeout):
        self.calls.append(("get_book", request.model_dump(mode="json", exclude_none=True)))
        if self.fail: raise TimeoutError("synthetic catalog timeout")
        book = self.books.get(request.book_id) if request.book_id else next((book for book in self.books.values() if book["isbn"] == request.isbn), None)
        if book is None: raise ToolError("BOOK_NOT_FOUND", "synthetic book not found")
        return deepcopy(book)


class ControlledKnowledge:
    def __init__(self, chunks):
        self.chunks = deepcopy(chunks)
        self.calls = []

    async def search(self, query, limit=5):
        self.calls.append(query)
        return deepcopy(self.chunks[:limit])
