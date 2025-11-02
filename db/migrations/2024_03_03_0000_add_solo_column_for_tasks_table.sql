-- 居住形態　※jusetsu-mci-detectionにもあるが、同じもの。
ALTER TABLE `task_houses` ADD COLUMN `solo` tinyint NULL COMMENT '1:単身, 0:それ以外' AFTER `education`;
