ALTER TABLE `task_houses` CHANGE COLUMN `age` `age` smallint unsigned NULL COMMENT '年齢';
ALTER TABLE `task_houses` CHANGE COLUMN `sex` `sex` smallint unsigned NULL COMMENT '1:man, 2:woman';
ALTER TABLE `task_houses` CHANGE COLUMN `education` `education` varchar(100) NULL COMMENT '教育歴';
