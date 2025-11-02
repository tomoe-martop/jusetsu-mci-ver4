#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
debugフラグとログ出力の関係をテストするスクリプト
"""

from pred_mci import Predictor, PredictorWithLogging
import os

# パラメータ
age = 70
male = 0
edu = 12
solo = 1
csv_path = "csv/test_data.csv"

def test_debug_flag():
    print("=== debugフラグとログ出力の関係テスト ===")
    
    # ログファイルの初期サイズを記録
    initial_size = os.path.getsize('predictor.log') if os.path.exists('predictor.log') else 0
    print(f"初期ログファイルサイズ: {initial_size} bytes")
    print()
    
    # テスト1: Predictorクラス + debug=False
    print("1. Predictorクラス + debug=False")
    p1 = Predictor(
        lgb_models_dir_path="models/lgb/*.txt",
        logi_models_dir_path="models/logistic/*.pkl",
        lgb_scaler_path="scaler/lgb_scaler.pickle",
        logi_scaler_path="scaler/logi_scaler.pickle"
    )
    res1 = p1.calculate_score(age, male, edu, solo, csv_path, debug=False)
    size1 = os.path.getsize('predictor.log')
    print(f"   結果: {res1}")
    print(f"   ログファイルサイズ: {size1} bytes (変化: {size1 - initial_size})")
    print()
    
    # テスト2: Predictorクラス + debug=True
    print("2. Predictorクラス + debug=True")
    p2 = Predictor(
        lgb_models_dir_path="models/lgb/*.txt",
        logi_models_dir_path="models/logistic/*.pkl",
        lgb_scaler_path="scaler/lgb_scaler.pickle",
        logi_scaler_path="scaler/logi_scaler.pickle"
    )
    res2 = p2.calculate_score(age, male, edu, solo, csv_path, debug=True)
    size2 = os.path.getsize('predictor.log')
    print(f"   結果: {res2}")
    print(f"   ログファイルサイズ: {size2} bytes (変化: {size2 - size1})")
    print()
    
    # テスト3: PredictorWithLoggingクラス + debug=False
    print("3. PredictorWithLoggingクラス + debug=False")
    p3 = PredictorWithLogging(
        lgb_models_dir_path="models/lgb/*.txt",
        logi_models_dir_path="models/logistic/*.pkl",
        lgb_scaler_path="scaler/lgb_scaler.pickle",
        logi_scaler_path="scaler/logi_scaler.pickle"
    )
    res3 = p3.calculate_score(age, male, edu, solo, csv_path, debug=False)
    size3 = os.path.getsize('predictor.log')
    print(f"   結果: {res3}")
    print(f"   ログファイルサイズ: {size3} bytes (変化: {size3 - size2})")
    print()
    
    # テスト4: PredictorWithLoggingクラス + debug=True
    print("4. PredictorWithLoggingクラス + debug=True")
    p4 = PredictorWithLogging(
        lgb_models_dir_path="models/lgb/*.txt",
        logi_models_dir_path="models/logistic/*.pkl",
        lgb_scaler_path="scaler/lgb_scaler.pickle",
        logi_scaler_path="scaler/logi_scaler.pickle"
    )
    res4 = p4.calculate_score(age, male, edu, solo, csv_path, debug=True)
    size4 = os.path.getsize('predictor.log')
    print(f"   結果: {res4}")
    print(f"   ログファイルサイズ: {size4} bytes (変化: {size4 - size3})")
    print()
    
    # 結論
    print("=== 結論 ===")
    print(f"Predictorクラス + debug=False: ログ変化 {size1 - initial_size} bytes")
    print(f"Predictorクラス + debug=True: ログ変化 {size2 - size1} bytes")
    print(f"PredictorWithLoggingクラス + debug=False: ログ変化 {size3 - size2} bytes")
    print(f"PredictorWithLoggingクラス + debug=True: ログ変化 {size4 - size3} bytes")
    
    if size1 - initial_size == 0 and size2 - size1 == 0:
        print("✅ Predictorクラスではdebugフラグに関係なくログは出力されません")
    else:
        print("❌ Predictorクラスでログが出力されました")
    
    if size3 - size2 > 0 and size4 - size3 > 0:
        print("✅ PredictorWithLoggingクラスではdebugフラグに関係なくログが出力されます")
    else:
        print("❌ PredictorWithLoggingクラスでログが出力されませんでした")

if __name__ == "__main__":
    test_debug_flag()

