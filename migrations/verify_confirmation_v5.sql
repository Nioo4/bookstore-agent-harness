CREATE TEMPORARY TABLE `_verify_confirmation_v5_guard`
(
  `valid` tinyint unsigned NOT NULL,
  CONSTRAINT `ck_verify_confirmation_v5_guard` CHECK (`valid` = 1)
) ENGINE=InnoDB;

INSERT INTO `_verify_confirmation_v5_guard` (`valid`)
SELECT 0
WHERE NOT EXISTS (
  SELECT 1 FROM `schema_migrations` WHERE `version` = 5 AND `dirty` = 0
);

INSERT INTO `_verify_confirmation_v5_guard` (`valid`)
SELECT 0
WHERE (
  SELECT COUNT(*)
  FROM `information_schema`.`columns`
  WHERE `table_schema` = DATABASE()
    AND `table_name` = 'pending_action'
    AND `column_name` IN (
      'pending_action_id', 'token_hash', 'payload_hash', 'quoted_amount',
      'prepare_idempotency_key', 'consumed_at', 'action_type', 'status',
      'decision', 'order_id'
    )
) <> 10;

INSERT INTO `_verify_confirmation_v5_guard` (`valid`)
SELECT 0
WHERE EXISTS (
  SELECT 1
  FROM `information_schema`.`columns`
  WHERE `table_schema` = DATABASE()
    AND `table_name` = 'pending_action'
    AND `column_name` IN (
      'action_id', 'confirmation_digest', 'items_hash', 'total_amount',
      'idempotency_key', 'used_at'
    )
);

INSERT INTO `_verify_confirmation_v5_guard` (`valid`)
SELECT 0
WHERE NOT EXISTS (
  SELECT 1
  FROM `information_schema`.`key_column_usage`
  WHERE `constraint_schema` = DATABASE()
    AND `table_name` = 'pending_action'
    AND `constraint_name` = 'fk_pending_order'
    AND `column_name` = 'order_id'
    AND `referenced_table_name` = 'customer_order'
    AND `referenced_column_name` = 'order_id'
);

INSERT INTO `_verify_confirmation_v5_guard` (`valid`)
SELECT 0
WHERE NOT EXISTS (
  SELECT 1
  FROM (
    SELECT `index_name`, `non_unique`,
           GROUP_CONCAT(`column_name` ORDER BY `seq_in_index` SEPARATOR ',') AS `columns_in_order`
    FROM `information_schema`.`statistics`
    WHERE `table_schema` = DATABASE() AND `table_name` = 'pending_action'
    GROUP BY `index_name`, `non_unique`
  ) AS `indexes`
  WHERE `indexes`.`index_name` = 'uk_pending_token_hash'
    AND `indexes`.`non_unique` = 0
    AND `indexes`.`columns_in_order` = 'token_hash'
);

INSERT INTO `_verify_confirmation_v5_guard` (`valid`)
SELECT 0
WHERE NOT EXISTS (
  SELECT 1
  FROM (
    SELECT `index_name`, `non_unique`,
           GROUP_CONCAT(`column_name` ORDER BY `seq_in_index` SEPARATOR ',') AS `columns_in_order`
    FROM `information_schema`.`statistics`
    WHERE `table_schema` = DATABASE() AND `table_name` = 'pending_action'
    GROUP BY `index_name`, `non_unique`
  ) AS `indexes`
  WHERE `indexes`.`index_name` = 'uk_pending_user_prepare_idempotency'
    AND `indexes`.`non_unique` = 0
    AND `indexes`.`columns_in_order` = 'user_id,prepare_idempotency_key'
);

INSERT INTO `_verify_confirmation_v5_guard` (`valid`)
SELECT 0
WHERE (
  SELECT COUNT(*)
  FROM `information_schema`.`table_constraints`
  WHERE `constraint_schema` = DATABASE()
    AND `table_name` = 'pending_action'
    AND `constraint_type` = 'CHECK'
    AND `constraint_name` IN (
      'ck_pending_action_type', 'ck_pending_status', 'ck_pending_decision'
    )
) <> 3;

INSERT INTO `_verify_confirmation_v5_guard` (`valid`)
SELECT 0
WHERE NOT EXISTS (
  SELECT 1
  FROM `information_schema`.`table_constraints` AS `constraints`
  JOIN `information_schema`.`check_constraints` AS `checks`
    ON `checks`.`constraint_schema` = `constraints`.`constraint_schema`
   AND `checks`.`constraint_name` = `constraints`.`constraint_name`
  WHERE `constraints`.`constraint_schema` = DATABASE()
    AND `constraints`.`table_name` = 'pending_action'
    AND `constraints`.`constraint_type` = 'CHECK'
    AND `constraints`.`constraint_name` = 'ck_pending_total_nonnegative'
    AND LOWER(`checks`.`check_clause`) LIKE '%quoted_amount%'
    AND LOWER(`checks`.`check_clause`) NOT LIKE '%total_amount%'
    AND REPLACE(REPLACE(REPLACE(LOWER(`checks`.`check_clause`), '`', ''), ' ', ''), '\n', '')
        LIKE '%quoted_amount>=0%'
);

DROP TEMPORARY TABLE `_verify_confirmation_v5_guard`;
