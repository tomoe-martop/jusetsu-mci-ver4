ALTER TABLE `tasks` ADD `algorithm` smallint NOT NULL DEFAULT 1 COMMENT '1:旧アルゴリズム, 2:新アルゴリズム' AFTER `type`;
