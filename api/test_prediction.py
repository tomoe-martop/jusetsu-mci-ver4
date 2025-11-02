#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
READMEの実装サンプル通りにPredictorクラスをテストするスクリプト
"""

from pred_mci import Predictor

# READMEの実装サンプル通り
age = 70
male = 0
edu = 12
solo = 1
csv_path = "csv/test_data.csv"

print("=== MCI予測テスト ===")
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
    
    # 予測実行
    print("予測を実行中...")
    res = p.calculate_score(age, male, edu, solo, csv_path)
    
    print("=== 結果 ===")
    print(f"ステータスコード: {res['status_code']}")
    print(f"スコア: {res['score']}")
    
    # ステータスコードの意味を表示
    status_messages = {
        100: "予測成功",
        200: "CSVファイルが見つかりません",
        201: "電力データフォーマットエラー",
        202: "必要な電力データ量を満たしていません",
        203: "電力データが空です",
        211: "背景データフォーマットエラー",
        300: "電力モデルが見つかりません",
        301: "電力モデル読み込みエラー",
        302: "電力モデル予測時のエラー",
        310: "背景モデルが見つかりません",
        311: "背景モデル読み込みエラー",
        312: "背景モデル予測時のエラー",
        400: "予測時のタイムアウト",
        900: "予期せぬエラー"
    }
    
    if res['status_code'] in status_messages:
        print(f"意味: {status_messages[res['status_code']]}")
    
    if res['status_code'] == 100:
        print(f"認知機能スコア: {res['score']}")
        if res['score'] is not None:
            if res['score'] >= 80:
                print("判定: 正常")
            elif res['score'] >= 60:
                print("判定: 軽度認知障害の可能性")
            else:
                print("判定: 認知症の可能性")
    
except Exception as e:
    print(f"エラーが発生しました: {e}")
    import traceback
    traceback.print_exc()

