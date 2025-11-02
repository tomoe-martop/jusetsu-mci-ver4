#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
debugモードでログ出力を確認するスクリプト
"""

from pred_mci import PredictorWithLogging
import os

# READMEの実装サンプル通り
age = 70
male = 0
edu = 12
solo = 1
csv_path = "csv/test_data.csv"

print("=== Debugモードでログ出力テスト ===")
print(f"現在のログファイルサイズ: {os.path.getsize('predictor.log') if os.path.exists('predictor.log') else 0} bytes")
print()

try:
    # PredictorWithLoggingを使用（ログ出力機能付き）
    print("PredictorWithLoggingクラスを初期化中...")
    p = PredictorWithLogging(
        lgb_models_dir_path="models/lgb/*.txt",
        logi_models_dir_path="models/logistic/*.pkl",
        lgb_scaler_path="scaler/lgb_scaler.pickle",
        logi_scaler_path="scaler/logi_scaler.pickle"
    )
    print("初期化完了")
    
    # 通常モードで実行
    print("通常モードで予測を実行中...")
    res_normal = p.calculate_score(age, male, edu, solo, csv_path, debug=False)
    print(f"通常モード結果: {res_normal}")
    
    print(f"通常モード実行後のログファイルサイズ: {os.path.getsize('predictor.log')} bytes")
    print()
    
    # Debugモードで実行
    print("Debugモードで予測を実行中...")
    res_debug = p.calculate_score(age, male, edu, solo, csv_path, debug=True)
    print(f"Debugモード結果: {res_debug}")
    
    print(f"Debugモード実行後のログファイルサイズ: {os.path.getsize('predictor.log')} bytes")
    print()
    
    # ログファイルの内容を表示
    print("=== ログファイルの内容 ===")
    if os.path.exists('predictor.log'):
        with open('predictor.log', 'r', encoding='utf-8') as f:
            log_content = f.read()
            if log_content:
                print(log_content)
            else:
                print("ログファイルは空です")
    else:
        print("ログファイルが存在しません")
    
except Exception as e:
    print(f"エラーが発生しました: {e}")
    import traceback
    traceback.print_exc()

