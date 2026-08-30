"""Append-only local demo catalog seeding; no models, accounts or workflow results."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "deploy/datasets/manual-v1.json"
PROJECT = "bookstore-live-local"
CONTAINER = PROJECT + "-mysql-1"


def load_dataset(path: Path = DATASET) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_dataset(data)
    return data


def validate_dataset(data: dict) -> None:
    if (data.get("dataset") != "bookstore-manual-v1" or data.get("currency") != "CNY"
            or data.get("price_unit") != "fen"):
        raise ValueError("INVALID_DATASET_HEADER")
    aliases, isbns = set(), set()
    for book in data["books"]:
        alias, isbn = book["alias"], book["isbn"]
        if not re.fullmatch(r"B[1-9][0-9]*", alias) or alias in aliases:
            raise ValueError("INVALID_OR_DUPLICATE_ALIAS")
        if not re.fullmatch(r"[0-9]{13}", isbn) or isbn in isbns or isbn == data["absent_isbn"]:
            raise ValueError("INVALID_OR_DUPLICATE_ISBN")
        aliases.add(alias)
        isbns.add(isbn)
        for field in ("title", "language_code"):
            if not isinstance(book[field], str) or not 1 <= len(book[field]) <= (10 if field == "language_code" else 255):
                raise ValueError("INVALID_BOOK_TEXT")
        for field in ("price", "stock"):
            if type(book[field]) is not int or not 0 <= book[field] <= 1_000_000:
                raise ValueError("INVALID_PRICE_OR_STOCK")
        if book["status"] not in {"ACTIVE", "INACTIVE", "DRAFT"}:
            raise ValueError("INVALID_STATUS")
        for field in ("authors", "categories"):
            values = book[field]
            if not isinstance(values, list) or len(values) != len(set(values)):
                raise ValueError("INVALID_RELATION_LIST")
            if any(not isinstance(v, str) or not 1 <= len(v) <= 255 for v in values):
                raise ValueError("INVALID_RELATION_TEXT")
        if any(c not in data["categories"] for c in book["categories"]):
            raise ValueError("UNDEFINED_CATEGORY")
        for field, maximum in (("subtitle", 255), ("description", 5000), ("publisher", 255)):
            if field in book and (not isinstance(book[field], str) or len(book[field]) > maximum):
                raise ValueError("INVALID_OPTIONAL_TEXT")
        if "page_count" in book and (type(book["page_count"]) is not int or not 1 <= book["page_count"] <= 10000):
            raise ValueError("INVALID_PAGE_COUNT")
        if "publication_date" in book:
            date.fromisoformat(book["publication_date"])
    if not aliases:
        raise ValueError("EMPTY_DATASET")
    if any(not re.fullmatch(r"[a-z][a-z0-9-]*", slug) or not isinstance(name, str)
           or not 1 <= len(name) <= 255 for slug, name in data["categories"].items()):
        raise ValueError("INVALID_CATEGORY")


def literal(value) -> str:
    """UTF-8 hex avoids SQL escaping and Windows console-encoding ambiguity."""
    if value is None:
        return "NULL"
    if type(value) is int:
        return str(value)
    if isinstance(value, str):
        return "CONVERT(X'" + value.encode("utf-8").hex() + "' USING utf8mb4)"
    raise ValueError("UNSUPPORTED_SQL_VALUE")


def command(parts: list[str], statement: str | None = None) -> str:
    result = subprocess.run(parts, input=statement, text=True, encoding="utf-8",
                            capture_output=True, cwd=ROOT, timeout=60)
    if result.returncode:
        # Docker/mysql diagnostics may contain SQL or environment data; don't echo them.
        raise RuntimeError("LOCAL_DATABASE_COMMAND_FAILED; no success report emitted")
    return result.stdout.strip()


def verify_target() -> None:
    raw = command(["docker", "inspect", "--format",
                   '{"labels":{{json .Config.Labels}},"running":{{json .State.Running}}}', CONTAINER])
    info = json.loads(raw)
    labels = info["labels"]
    if (not info["running"] or labels.get("com.docker.compose.project") != PROJECT
            or labels.get("com.docker.compose.service") != "mysql"
            or Path(labels.get("com.docker.compose.project.working_dir", "")).resolve() != ROOT):
        raise RuntimeError("REFUSING_WRONG_OR_STOPPED_LOCAL_PROJECT")


def sql(statement: str) -> str:
    return command(["docker", "exec", "-i", CONTAINER, "sh", "-c",
                    'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysql -uroot '
                    '-D "$MYSQL_DATABASE" --default-character-set=utf8mb4 --batch --raw --skip-column-names'], statement)


def snapshot(data: dict) -> dict:
    isbns = ",".join(literal(b["isbn"]) for b in data["books"])
    output = sql("SELECT JSON_OBJECT('isbn',b.isbn,'book_id',b.book_id,'title',b.title,"
                 "'price',b.price,'stock',i.available_stock,'status',b.status,'version',b.version,"
                 "'inventory_version',i.version) FROM book b LEFT JOIN inventory i ON i.book_id=b.book_id "
                 f"WHERE b.isbn IN ({isbns});")
    return {row["isbn"]: row for row in (json.loads(line) for line in output.splitlines())}


def build_insert_sql(data: dict, existing: dict) -> str:
    """Only missing ISBNs get rows/relations. Never repair/reset an existing book."""
    statements = ["SET NAMES utf8mb4;", "START TRANSACTION;"]
    for book in data["books"]:
        if book["isbn"] in existing:
            continue
        fields = ["isbn", "title", "subtitle", "description", "publisher", "publication_date",
                  "language_code", "page_count", "price", "status"]
        values = [literal(book.get(f)) for f in fields]
        statements.append("INSERT INTO book (" + ",".join(fields) + ",author,stock_count,currency) VALUES ("
                          + ",".join(values + [literal("、".join(book["authors"])), str(book["stock"]), "'CNY'"]) + ");")
        statements.append("SET @seed_book_id=LAST_INSERT_ID();")
        statements.append(f"INSERT INTO inventory (book_id,available_stock) VALUES (@seed_book_id,{book['stock']});")
        for index, name in enumerate(book["authors"]):
            name_sql = literal(name)
            statements.append(f"INSERT INTO author (name) SELECT {name_sql} WHERE NOT EXISTS (SELECT 1 FROM author WHERE name={name_sql});")
            statements.append("INSERT INTO book_author (book_id,author_id,sort_order) "
                              f"SELECT @seed_book_id,author_id,{index} FROM author WHERE name={name_sql};")
        for slug in book["categories"]:
            slug_sql, name_sql = literal(slug), literal(data["categories"][slug])
            statements.append(f"INSERT INTO category (slug,name) SELECT {slug_sql},{name_sql} WHERE NOT EXISTS (SELECT 1 FROM category WHERE slug={slug_sql});")
            statements.append("INSERT INTO book_category (book_id,category_id) "
                              f"SELECT @seed_book_id,category_id FROM category WHERE slug={slug_sql};")
    statements.append("COMMIT;")
    return "\n".join(statements)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Insert missing ISBNs into this checkout's bookstore-live-local only")
    args = parser.parse_args()
    data = load_dataset()
    verify_target()
    before = snapshot(data)
    if any(row["stock"] is None for row in before.values()):
        raise RuntimeError("EXISTING_BOOK_HAS_NO_INVENTORY; manual inspection required")
    missing = [b for b in data["books"] if b["isbn"] not in before]
    if args.apply and missing:
        sql(build_insert_sql(data, before))
    after = snapshot(data) if args.apply else before
    if args.apply and any(b["isbn"] not in after for b in data["books"]):
        raise RuntimeError("POST_IMPORT_CATALOG_INCOMPLETE")
    print(json.dumps({"dataset": data["dataset"], "project": PROJECT,
                      "mode": "apply" if args.apply else "read_only",
                      "inserted": len(missing) if args.apply else 0,
                      "missing_before": len(missing), "existing_before": len(before),
                      "books": [{"alias": b["alias"], **after[b["isbn"]]} for b in data["books"] if b["isbn"] in after]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
