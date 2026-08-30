CREATE TEMPORARY TABLE `_migration_000006_confirmation_guard`
(
  `valid` tinyint unsigned NOT NULL,
  CONSTRAINT `ck_migration_000006_confirmation_guard` CHECK (`valid` = 1)
) ENGINE=InnoDB;

INSERT INTO `_migration_000006_confirmation_guard` (`valid`)
SELECT 0
FROM `pending_action` AS `pending`
LEFT JOIN `customer_order` AS `orders`
  ON `orders`.`user_id` = `pending`.`user_id`
 AND `orders`.`idempotency_key` = `pending`.`prepare_idempotency_key`
WHERE `pending`.`status` = 'PENDING'
  AND `pending`.`consumed_at` IS NOT NULL
  AND `orders`.`order_id` IS NULL
LIMIT 1;

DROP TEMPORARY TABLE `_migration_000006_confirmation_guard`;

UPDATE `pending_action` AS `pending`
JOIN `customer_order` AS `orders`
  ON `orders`.`user_id` = `pending`.`user_id`
 AND `orders`.`idempotency_key` = `pending`.`prepare_idempotency_key`
SET `pending`.`status` = 'APPROVED',
    `pending`.`decision` = 'APPROVE',
    `pending`.`order_id` = `orders`.`order_id`
WHERE `pending`.`status` = 'PENDING'
  AND `pending`.`consumed_at` IS NOT NULL;

ALTER TABLE `pending_action`
  ADD CONSTRAINT `ck_pending_state_consistency` CHECK (
    (`status` = 'PENDING' AND `decision` IS NULL AND `order_id` IS NULL AND `consumed_at` IS NULL)
    OR (`status` = 'APPROVED' AND `decision` = 'APPROVE' AND `order_id` IS NOT NULL AND `consumed_at` IS NOT NULL)
    OR (`status` = 'REJECTED' AND `decision` = 'REJECT' AND `order_id` IS NULL AND `consumed_at` IS NOT NULL)
    OR (`status` = 'EXPIRED' AND `decision` IS NULL AND `order_id` IS NULL AND `consumed_at` IS NOT NULL)
  );
