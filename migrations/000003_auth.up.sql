CREATE TABLE `user_account`
(
  `user_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `email` varchar(320) NOT NULL,
  `display_name` varchar(100) NOT NULL,
  `password_hash` varchar(255) NOT NULL,
  `email_verified_at` datetime(3) NULL,
  `status` varchar(16) NOT NULL DEFAULT 'ACTIVE',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`user_id`),
  UNIQUE KEY `uk_user_account_email` (`email`),
  CONSTRAINT `ck_user_account_status` CHECK (`status` IN ('ACTIVE', 'DISABLED'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `email_verification_token`
(
  `token_digest` binary(32) NOT NULL,
  `user_id` bigint unsigned NOT NULL,
  `expires_at` datetime(3) NOT NULL,
  `used_at` datetime(3) NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`token_digest`),
  KEY `idx_email_verification_user_created` (`user_id`, `created_at`),
  KEY `idx_email_verification_expiry` (`expires_at`),
  CONSTRAINT `fk_email_verification_user` FOREIGN KEY (`user_id`)
    REFERENCES `user_account` (`user_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

