#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
デバッグモードで生の結果を出力するスクリプト
"""

from pred_mci import Predictor

# READMEの実装サンプル通り
age = 70
male = 0
edu = 12
solo = 1
csv_path = "csv/test_data.csv"

print("=== MCI予測テスト（デバッグモード） ===")
print(f"年齢: {age}")
print(f"性別: {'男性' if male == 1 else '女性'}")
print(f"教育年数: {edu}")
print(f"独居: {'はい' if solo == 1 else 'いいえ'}")
print(f"CSVファイル: {csv_path}")
print()

try:
    # インスタンス化
    print("Predictorクラスを初期化中...")
    p = Predictor(
        lgb_models_dir_path="models/lgb/*.txt",
        logi_models_dir_path="models/logistic/*.pkl",
        lgb_scaler_path="scaler/lgb_scaler.pickle",
        logi_scaler_path="scaler/logi_scaler.pickle"
    )
    print("初期化完了")
    
    # デバッグモードで予測実行
    print("デバッグモードで予測を実行中...")
    res = p.calculate_score(age, male, edu, solo, csv_path, debug=True)
    
    print("=== 生の結果（デバッグモード） ===")
    print(f"結果オブジェクト: {res}")
    print(f"結果の型: {type(res)}")
    print()
    
    if isinstance(res, dict):
        print("=== 詳細結果 ===")
        for key, value in res.items():
            print(f"{key}: {value} (型: {type(value)})")
        
        if 'score' in res and isinstance(res['score'], dict):
            print("\n=== 各モデルの予測値 ===")
            for model_name, score in res['score'].items():
                print(f"{model_name}: {score} (型: {type(score)})")
    
    print("\n=== 通常モードでの結果 ===")
    res_normal = p.calculate_score(age, male, edu, solo, csv_path, debug=False)
    print(f"通常モード結果: {res_normal}")
    
except Exception as e:
    print(f"エラーが発生しました: {e}")
    import traceback
    traceback.print_exc()

