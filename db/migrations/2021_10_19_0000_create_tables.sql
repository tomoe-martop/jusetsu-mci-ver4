CREATE TABLE `tasks` (
  `id` int unsigned NOT NULL AUTO_INCREMENT,
  `type` smallint NOT NULL COMMENT '1:定時バッチ, 2:即時バッチ',
  `date_from` date NOT NULL,
  `date_to` date NOT NULL,
  `starting_at` datetime NOT NULL COMMENT '実行させたい日時（即時実行の場合は過去日時を登録すれば良い）',
  `start_at` datetime NULL,
  `end_at` datetime NULL,
  `status` tinyint NOT NULL DEFAULT 0 COMMENT '実行結果（エラーの場合マイナスなど）',
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `task_houses` (
  `id` int unsigned NOT NULL AUTO_INCREMENT,
  `task_id` int unsigned NOT NULL,
  `spid` varchar(4) COLLATE utf8mb4_bin NOT NULL COMMENT '販社ID',
  `houseid` varchar(12) COLLATE utf8mb4_bin NOT NULL COMMENT 'ハウスID',
  `age` smallint unsigned NOT NULL COMMENT '年齢',
  `sex` smallint unsigned NOT NULL COMMENT '1:man, 2:woman',
  `education` varchar(100) NOT NULL COMMENT '教育歴',
  `status` tinyint NOT NULL DEFAULT 0 COMMENT '実行結果（エラーの場合マイナスなど）',
  `progress` smallint NOT NULL DEFAULT 0 COMMENT '進捗0-100',
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `task_houses_unique_task_spid_houseid` (`task_id`, `spid`,`houseid`),
  FOREIGN KEY `task_houses_foreign_task_id`(`task_id`) REFERENCES `tasks`(`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `task_results` (
  `id` int unsigned NOT NULL AUTO_INCREMENT,
  `task_id` int unsigned NOT NULL,
  `task_house_id` int unsigned NOT NULL,
  `result` tinyint NOT NULL COMMENT '-1（エラー）, 0, 1',
  `alerted_sps_at` datetime NULL COMMENT '事業者へのアラート日時（対象がなくても抽出済みの場合はupdateすること）',
  `alerted_houses_at` datetime NULL COMMENT 'ハウスへのアラート日時（対象がなくても抽出済みの場合はupdateすること）',
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `task_results_unique_task_house_id` (`task_house_id`),
  FOREIGN KEY `task_results_foreign_task_id`(`task_id`) REFERENCES `tasks`(`id`),
  FOREIGN KEY `task_results_foreign_task_house_id`(`task_house_id`) REFERENCES `task_houses`(`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- DROP TABLE data_histories;
-- ALTER TABLE `task_results` ADD COLUMN `created_at` datetime NOT NULL;
