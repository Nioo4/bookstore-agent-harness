DROP TABLE IF EXISTS `inventory`;
DROP TABLE IF EXISTS `book_category`;
DROP TABLE IF EXISTS `category`;
DROP TABLE IF EXISTS `book_author`;
DROP TABLE IF EXISTS `author`;

ALTER TABLE `book`
  DROP INDEX `idx_book_language_price_id`,
  DROP INDEX `idx_book_status_date_id`,
  DROP INDEX `ft_book_search`,
  DROP CHECK `ck_book_status`,
  DROP CHECK `ck_book_page_count_positive`,
  DROP CHECK `ck_book_price_nonnegative`,
  DROP COLUMN `updated_at`,
  DROP COLUMN `created_at`,
  DROP COLUMN `version`,
  DROP COLUMN `status`,
  DROP COLUMN `currency`,
  DROP COLUMN `cover_url`,
  DROP COLUMN `page_count`,
  DROP COLUMN `language_code`,
  DROP COLUMN `publication_date`,
  DROP COLUMN `publisher`,
  DROP COLUMN `description`,
  DROP COLUMN `subtitle`;

