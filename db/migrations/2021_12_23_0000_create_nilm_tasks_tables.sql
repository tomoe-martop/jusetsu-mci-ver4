CREATE TABLE `nilm_tasks` (
  `id` int unsigned NOT NULL AUTO_INCREMENT,
  `date_from` date NOT NULL,
  `date_to` date NOT NULL,
  `start_at` datetime NULL,
  `end_at` datetime NULL,
  `status` tinyint NOT NULL DEFAULT 0 COMMENT '実行結果（エラーの場合マイナスなど）',
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `nilm_task_houses` (
  `id` int unsigned NOT NULL AUTO_INCREMENT,
  `nilm_task_id` int unsigned NOT NULL,
  `spid` varchar(4) COLLATE utf8mb4_bin NOT NULL COMMENT '販社ID',
  `houseid` varchar(12) COLLATE utf8mb4_bin NOT NULL COMMENT 'ハウスID',
  `status` tinyint NOT NULL DEFAULT 0 COMMENT '実行結果（エラーの場合マイナスなど）',
  `progress` smallint NOT NULL DEFAULT 0 COMMENT '進捗0-100',
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `nilm_task_houses_unique_task_spid_houseid` (`nilm_task_id`, `spid`,`houseid`),
  FOREIGN KEY `nilm_task_houses_foreign_nilm_task_id`(`nilm_task_id`) REFERENCES `nilm_tasks`(`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
