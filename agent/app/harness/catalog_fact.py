"""Project exact catalog facts, never model prose or semantic recommendations."""
import re


def project_catalog_fact(action, results, request, question_ids):
    if set(question_ids) != {"catalog_fact"}:
        return
    identifiers = re.findall(r"ISBN\s*(?:为|是|:|：)?\s*([0-9Xx-]{10,17})", request, re.I)
    if len(identifiers) != 1:
        return
    isbn = identifiers[0].replace("-", "").upper()
    if len(isbn) not in (10, 13):
        return
    matching = {}
    for result in results:
        if result.get("tool") not in {"get_book", "search_books"}:
            continue
        value = result.get("value")
        if not isinstance(value, dict):
            continue
        books = value.get("books", [value.get("book", value)])
        for book in books if isinstance(books, list) else []:
            if isinstance(book, dict) and str(book.get("isbn", "")).replace("-", "").upper() == isbn:
                matching[book.get("book_id")] = book
    if len(matching) != 1:
        return
    book = next(iter(matching.values()))
    price, stock = book.get("price"), book.get("available_stock")
    if type(price) is not int or price < 0 or type(stock) is not int or stock < 0 or book.get("currency") != "CNY":
        return
    # Discard unsolicited semantic prose as well as cards; never merely bypass validation.
    action.answer = f"ISBN {isbn}：价格 {price // 100}.{price % 100:02d} 元，当前库存 {stock} 本。以上为本次实时商品查询结果。"
    action.recommendations = []
    action.policy_claims = []
    action.addressed_question_ids = ["catalog_fact"]
