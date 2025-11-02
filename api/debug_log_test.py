#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
debug=Trueとdebug=Falseでログ出力の違いを確認するスクリプト
"""

from pred_mci import Predictor
import os

# READMEの実装サンプル通り
age = 70
male = 0
edu = 12
solo = 1
csv_path = "csv/test_data.csv"

print("=== Debugモードとログ出力の関係確認 ===")
print(f"現在のログファイルサイズ: {os.path.getsize('predictor.log') if os.path.exists('predictor.log') else 0} bytes")
print()

try:
    # Predictorクラスを初期化
    print("Predictorクラスを初期化中...")
    p = Predictor(
        lgb_models_dir_path="models/lgb/*.txt",
        logi_models_dir_path="models/logistic/*.pkl",
        lgb_scaler_path="scaler/lgb_scaler.pickle",
        logi_scaler_path="scaler/logi_scaler.pickle"
    )
    print("初期化完了")
    
    # 通常モード（debug=False）で実行
    print("\n1. 通常モード（debug=False）で実行...")
    res_normal = p.calculate_score(age, male, edu, solo, csv_path, debug=False)
    print(f"通常モード結果: {res_normal}")
    print(f"通常モード実行後のログファイルサイズ: {os.path.getsize('predictor.log')} bytes")
    
    # ログファイルの内容を確認
    with open('predictor.log', 'r', encoding='utf-8') as f:
        log_content_normal = f.read()
    print(f"通常モード後のログ行数: {len(log_content_normal.splitlines())}")
    
    # Debugモード（debug=True）で実行
    print("\n2. Debugモード（debug=True）で実行...")
    res_debug = p.calculate_score(age, male, edu, solo, csv_path, debug=True)
    print(f"Debugモード結果: {res_debug}")
    print(f"Debugモード実行後のログファイルサイズ: {os.path.getsize('predictor.log')} bytes")
    
    # ログファイルの内容を確認
    with open('predictor.log', 'r', encoding='utf-8') as f:
        log_content_debug = f.read()
    print(f"Debugモード後のログ行数: {len(log_content_debug.splitlines())}")
    
    print("\n=== ログファイルの内容 ===")
    if log_content_debug:
        lines = log_content_debug.splitlines()
        for i, line in enumerate(lines[-10:], 1):  # 最後の10行を表示
            print(f"{i:2d}: {line}")
    else:
        print("ログファイルは空です")
    
    print("\n=== 結論 ===")
    if len(log_content_debug) > len(log_content_normal):
        print("✅ Debugモードでログが追加されました")
    else:
        print("❌ Debugモードでもログは追加されませんでした")
    
except Exception as e:
    print(f"エラーが発生しました: {e}")
    import traceback
    traceback.print_exc()

