#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
実行時のパラメータを詳しく表示するスクリプト
"""

from pred_mci import Predictor
import numpy as np

# READMEの実装サンプル通り
age = 70
male = 0
edu = 12
solo = 1
csv_path = "csv/test_data.csv"

print("=== 実行時のパラメータ詳細 ===")
print()

# 1. クラス定数
print("1. Predictorクラスの定数:")
print(f"   N_DAY_ELECTRIC_DATA: {Predictor.N_DAY_ELECTRIC_DATA} (28日間)")
print(f"   N_MINUTES_PER_DAY: {Predictor.N_MINUTES_PER_DAY} (1440分 = 24時間)")
print(f"   N_ROWS_ELECTRIC_DATA: {Predictor.N_ROWS_ELECTRIC_DATA} (40,320行)")
print(f"   THRESHOLD_ELECTRIC_DATA: {Predictor.THRESHOLD_ELECTRIC_DATA} (95%)")
print(f"   N_DAY_LIMIT_ELECTRIC_DATA: {Predictor.N_DAY_LIMIT_ELECTRIC_DATA} (25日)")
print()

# 2. 入力パラメータ
print("2. 入力パラメータ:")
print(f"   age: {age} (年齢)")
print(f"   male: {male} (男性=1, 女性=0)")
print(f"   edu: {edu} (教育年数)")
print(f"   solo: {solo} (独居=1, 同居者あり=0)")
print(f"   csv_path: {csv_path}")
print()

# 3. 内部変換パラメータ
print("3. 内部変換パラメータ:")
sex = 1 if male == 1 else 2
sex_1 = sex == 1
sex_2 = sex == 2
edu_0 = edu > 9
edu_1 = edu <= 9
solo_0 = solo == 0
solo_1 = solo == 1

print(f"   sex: {sex} (1=男性, 2=女性)")
print(f"   sex_1: {sex_1} (男性かどうか)")
print(f"   sex_2: {sex_2} (女性かどうか)")
print(f"   edu_0: {edu_0} (教育年数>9年)")
print(f"   edu_1: {edu_1} (教育年数≤9年)")
print(f"   solo_0: {solo_0} (同居者あり)")
print(f"   solo_1: {solo_1} (独居)")
print()

# 4. 時間帯分割パラメータ
print("4. 時間帯分割パラメータ:")
nighttime_hour_0 = 5  # 0:00〜4:59
nighttime_hour_1 = 2  # 22:00〜23:59
daytime_minutes = (24 - nighttime_hour_0 - nighttime_hour_1) * 60
midnight_minutes = (nighttime_hour_0 + nighttime_hour_1) * 60

print(f"   nighttime_hour_0: {nighttime_hour_0} (0:00〜4:59)")
print(f"   nighttime_hour_1: {nighttime_hour_1} (22:00〜23:59)")
print(f"   daytime_minutes: {daytime_minutes} (5:00〜21:59)")
print(f"   midnight_minutes: {midnight_minutes} (深夜帯合計)")
print()

# 5. モデル数
print("5. モデル数:")
print(f"   LightGBMモデル: 500個")
print(f"   ロジスティック回帰モデル: 50個")
print()

# 6. SANITIZER配列
print("6. SANITIZER配列 (特徴量選択):")
print(f"   配列長: {len(Predictor.SANITIZER)}")
print(f"   Trueの数: {sum(Predictor.SANITIZER)}")
print(f"   Falseの数: {len(Predictor.SANITIZER) - sum(Predictor.SANITIZER)}")
print()

# 7. タイムアウト設定
print("7. タイムアウト設定:")
from myexception import TIMEOUT
print(f"   TIMEOUT: {TIMEOUT}秒")
print()

# 8. 実際の実行
try:
    print("8. 実際の実行:")
    p = Predictor(
        lgb_models_dir_path="models/lgb/*.txt",
        logi_models_dir_path="models/logistic/*.pkl",
        lgb_scaler_path="scaler/lgb_scaler.pickle",
        logi_scaler_path="scaler/logi_scaler.pickle"
    )
    
    # データ読み込み
    array_datetime, array_daytime, array_midnight = p._load_data(csv_path)
    print(f"   array_datetime shape: {array_datetime.shape}")
    print(f"   array_daytime shape: {array_daytime.shape}")
    print(f"   array_midnight shape: {array_midnight.shape}")
    
    # 特徴量作成
    elec_total = np.hstack([array_daytime, array_midnight])
    interactions = np.outer(array_datetime, elec_total).flatten()
    array_behavior = np.array([age, sex_1, sex_2, edu_0, edu_1, solo_0, solo_1])
    array_new = np.hstack([array_behavior, array_datetime, elec_total, interactions])
    
    print(f"   elec_total shape: {elec_total.shape}")
    print(f"   interactions shape: {interactions.shape}")
    print(f"   array_behavior shape: {array_behavior.shape}")
    print(f"   array_new shape: {array_new.shape}")
    
    # スケーリング
    X_scaled = p.lgb_scaler.transform(array_new.reshape(1, -1)).reshape(-1)
    array_sanitized = X_scaled[p.SANITIZER].reshape(1, -1)
    
    print(f"   X_scaled shape: {X_scaled.shape}")
    print(f"   array_sanitized shape: {array_sanitized.shape}")
    
    print("   実行完了")
    
except Exception as e:
    print(f"   エラー: {e}")

