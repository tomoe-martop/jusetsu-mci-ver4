-- 結果
-- select 100-result, result  from `task_results` where result >= 0
UPDATE `task_results` SET result=(100-result) where result >= 0;
