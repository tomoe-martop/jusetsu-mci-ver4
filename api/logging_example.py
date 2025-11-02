#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ログが出る場合の実装例（PredictorWithLoggingクラス）
"""

from pred_mci import PredictorWithLogging

# パラメータ
age = 70
male = 0
edu = 12
solo = 1
csv_path = "csv/test_data.csv"

print("=== ログが出る実装例（PredictorWithLogging） ===")

# PredictorWithLoggingクラスを使用（ログ出力あり）
p = PredictorWithLogging(
    lgb_models_dir_path="models/lgb/*.txt",
    logi_models_dir_path="models/logistic/*.pkl",
    lgb_scaler_path="scaler/lgb_scaler.pickle",
    logi_scaler_path="scaler/logi_scaler.pickle"
)

# 予測実行（ログがpredictor.logに出力される）
res = p.calculate_score(age, male, edu, solo, csv_path, debug=False)
print(f"結果: {res}")

print("ログファイル 'predictor.log' に詳細なログが出力されました。")

