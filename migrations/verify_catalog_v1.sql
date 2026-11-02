-- Run immediately after 000002_catalog_v1.up.sql and before accepting writes.
-- Every *_status value must be PASS. The totals provide a reviewable audit trail.

SELECT COUNT(*) AS book_count,
       COUNT(DISTINCT isbn) AS distinct_isbn_count,
       CASE WHEN COUNT(*) = COUNT(DISTINCT isbn) THEN 'PASS' ELSE 'FAIL' END AS isbn_status
FROM book;

SELECT COUNT(*) AS books_without_inventory,
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS inventory_relation_status
FROM book b
LEFT JOIN inventory i ON i.book_id = b.book_id
WHERE i.book_id IS NULL;

SELECT COUNT(*) AS legacy_authors_without_relation,
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS author_relation_status
FROM book b
LEFT JOIN book_author ba ON ba.book_id = b.book_id AND ba.role = 'AUTHOR'
WHERE TRIM(b.author) <> '' AND ba.book_id IS NULL;

SELECT (SELECT COALESCE(SUM(stock_count), 0) FROM book) AS legacy_stock_total,
       (SELECT COALESCE(SUM(available_stock), 0) FROM inventory) AS inventory_stock_total,
       CASE
         WHEN (SELECT COALESCE(SUM(stock_count), 0) FROM book) =
              (SELECT COALESCE(SUM(available_stock), 0) FROM inventory)
         THEN 'PASS' ELSE 'FAIL'
       END AS stock_total_status;

