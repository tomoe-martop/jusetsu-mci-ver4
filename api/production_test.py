#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本番モードでMCI予測を実行するスクリプト
"""

from pred_mci import Predictor
import time

# READMEの実装サンプル通り
age = 70
male = 0
edu = 12
solo = 1
csv_path = "csv/test_data.csv"

print("=== 本番モード MCI予測実行 ===")
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
    
    # Predictorクラスを初期化
    print("1. モデル初期化中...")
    p = Predictor(
        lgb_models_dir_path="models/lgb/*.txt",
        logi_models_dir_path="models/logistic/*.pkl",
        lgb_scaler_path="scaler/lgb_scaler.pickle",
        logi_scaler_path="scaler/logi_scaler.pickle"
    )
    init_time = time.time() - start_time
    print(f"   初期化完了 (所要時間: {init_time:.2f}秒)")
    
    # 予測実行
    print("2. 予測実行中...")
    predict_start = time.time()
    res = p.calculate_score(age, male, edu, solo, csv_path, debug=False)
    predict_time = time.time() - predict_start
    print(f"   予測完了 (所要時間: {predict_time:.2f}秒)")
    
    # 総実行時間
    total_time = time.time() - start_time
    
    print()
    print("=== 予測結果 ===")
    print(f"ステータスコード: {res['status_code']}")
    
    # ステータスコードの意味を表示
    status_messages = {
        100: "✅ 予測成功",
        200: "❌ CSVファイルが見つかりません",
        201: "❌ 電力データフォーマットエラー",
        202: "❌ 必要な電力データ量を満たしていません",
        203: "❌ 電力データが空です",
        211: "❌ 背景データフォーマットエラー",
        300: "❌ 電力モデルが見つかりません",
        301: "❌ 電力モデル読み込みエラー",
        302: "❌ 電力モデル予測時のエラー",
        310: "❌ 背景モデルが見つかりません",
        311: "❌ 背景モデル読み込みエラー",
        312: "❌ 背景モデル予測時のエラー",
        400: "❌ 予測時のタイムアウト",
        900: "❌ 予期せぬエラー"
    }
    
    if res['status_code'] in status_messages:
        print(f"結果: {status_messages[res['status_code']]}")
    
    if res['status_code'] == 100:
        score = res['score']
        print(f"認知機能スコア: {score}")
        
        # 判定基準
        if score >= 80:
            print("判定: 🟢 正常")
        elif score >= 60:
            print("判定: 🟡 軽度認知障害の可能性")
        else:
            print("判定: 🔴 認知症の可能性")
    
    print()
    print("=== 実行統計 ===")
    print(f"初期化時間: {init_time:.2f}秒")
    print(f"予測時間: {predict_time:.2f}秒")
    print(f"総実行時間: {total_time:.2f}秒")
    
    print()
    print("=== 実行完了 ===")
    
except Exception as e:
    print(f"❌ エラーが発生しました: {e}")
    import traceback
    traceback.print_exc()

