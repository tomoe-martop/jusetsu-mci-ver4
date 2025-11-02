#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PredictorWithLoggingクラスでMCI予測を実行するスクリプト
"""

from pred_mci import PredictorWithLogging
import time

# READMEの実装サンプル通り
age = 70
male = 0
edu = 12
solo = 1
csv_path = "csv/test_data.csv"

print("=== PredictorWithLoggingクラス MCI予測実行 ===")
print(f"実行日時: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"年齢: {age}歳")
print(f"性別: {'男性' if male == 1 else '女性'}")
print(f"教育年数: {edu}年")
print(f"独居: {'はい' if solo == 1 else 'いいえ'}")
print(f"CSVファイル: {csv_path}")
print()

try:
    # 開始時間記録
    start_time = time.time()
    
    # PredictorWithLoggingクラスを初期化
    print("1. PredictorWithLoggingクラスを初期化中...")
    p = PredictorWithLogging(
        lgb_models_dir_path="models/lgb/*.txt",
        logi_models_dir_path="models/logistic/*.pkl",
        lgb_scaler_path="scaler/lgb_scaler.pickle",
        logi_scaler_path="scaler/logi_scaler.pickle"
    )
    init_time = time.time() - start_time
    print(f"   初期化完了 (所要時間: {init_time:.2f}秒)")
    
    # 通常モードで予測実行
    print("2. 通常モード（debug=False）で予測実行中...")
    predict_start = time.time()
    res_normal = p.calculate_score(age, male, edu, solo, csv_path, debug=False)
    predict_time = time.time() - predict_start
    print(f"   予測完了 (所要時間: {predict_time:.2f}秒)")
    
    # Debugモードで予測実行
    print("3. Debugモード（debug=True）で予測実行中...")
    debug_start = time.time()
    res_debug = p.calculate_score(age, male, edu, solo, csv_path, debug=True)
    debug_time = time.time() - debug_start
    print(f"   予測完了 (所要時間: {debug_time:.2f}秒)")
    
    # 総実行時間
    total_time = time.time() - start_time
    
    print()
    print("=== 予測結果 ===")
    print("通常モード結果:")
    print(f"  ステータスコード: {res_normal['status_code']}")
    print(f"  スコア: {res_normal['score']}")
    
    print("\nDebugモード結果:")
    print(f"  ステータスコード: {res_debug['status_code']}")
    print(f"  スコア: {res_debug['score']}")
    
    # 判定基準
    if res_normal['status_code'] == 100:
        score = res_normal['score']
        print(f"\n認知機能スコア: {score}")
        
        if score >= 80:
            print("判定: 🟢 正常")
        elif score >= 60:
            print("判定: 🟡 軽度認知障害の可能性")
        else:
            print("判定: 🔴 認知症の可能性")
    
    print()
    print("=== 実行統計 ===")
    print(f"初期化時間: {init_time:.2f}秒")
    print(f"通常モード予測時間: {predict_time:.2f}秒")
    print(f"Debugモード予測時間: {debug_time:.2f}秒")
    print(f"総実行時間: {total_time:.2f}秒")
    
    print()
    print("=== ログファイル情報 ===")
    import os
    if os.path.exists('predictor.log'):
        log_size = os.path.getsize('predictor.log')
        print(f"ログファイルサイズ: {log_size} bytes")
        print("ログファイル 'predictor.log' に詳細なログが出力されました")
    else:
        print("ログファイルが存在しません")
    
    print()
    print("=== 実行完了 ===")
    
except Exception as e:
    print(f"❌ エラーが発生しました: {e}")
    import traceback
    traceback.print_exc()
