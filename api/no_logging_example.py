#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ログが出ない場合の実装例（Predictorクラス）
"""

from pred_mci import Predictor

# パラメータ
age = 70
male = 0
edu = 12
solo = 1
csv_path = "csv/test_data.csv"

print("=== ログが出ない実装例（Predictor） ===")

# Predictorクラスを使用（ログ出力なし）
p = Predictor(
    lgb_models_dir_path="models/lgb/*.txt",
    logi_models_dir_path="models/logistic/*.pkl",
    lgb_scaler_path="scaler/lgb_scaler.pickle",
    logi_scaler_path="scaler/logi_scaler.pickle"
)

# 予測実行（ログは出力されない）
res = p.calculate_score(age, male, edu, solo, csv_path, debug=False)
print(f"結果: {res}")

print("ログファイル 'predictor.log' には何も出力されません。")

