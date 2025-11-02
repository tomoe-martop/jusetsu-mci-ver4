#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CSVファイルの日付フォーマットを変換するスクリプト
期待される形式: YYYY/MM/DD HH:MM
"""

import pandas as pd
import datetime
import os

def convert_csv_format(input_file, output_file):
    """
    CSVファイルの日付フォーマットを変換する
    
    Args:
        input_file (str): 入力CSVファイルのパス
        output_file (str): 出力CSVファイルのパス
    """
    
    print(f"入力ファイル: {input_file}")
    print(f"出力ファイル: {output_file}")
    
    # CSVファイルを読み込み
    df = pd.read_csv(input_file)
    
    print(f"元のデータ形状: {df.shape}")
    print(f"元の列名: {list(df.columns)}")
    
    # 日付列を正しい形式に変換
    df['date_time_jst'] = pd.to_datetime(df['date_time_jst']).dt.strftime('%Y/%m/%d %H:%M')
    
    # 必要な列のみを選択（refrigerator列を除外）
    required_columns = ['date_time_jst', 'air_conditioner', 'clothes_washer', 'microwave', 'rice_cooker', 'TV', 'cleaner', 'IH', 'Heater']
    
    # refrigerator列が存在する場合は除外
    if 'refrigerator' in df.columns:
        print("refrigerator列を除外します")
        df = df[required_columns]
    else:
        print("refrigerator列は存在しません")
        # 10列目が必要な場合は、空の列を追加
        if len(df.columns) == 9:
            df['dummy_column'] = 0
            print("10列目としてdummy_columnを追加しました")
    
    print(f"変換後のデータ形状: {df.shape}")
    print(f"変換後の列名: {list(df.columns)}")
    
    # 最初の5行を表示
    print("\n変換後の最初の5行:")
    print(df.head())
    
    # 最後の5行を表示
    print("\n変換後の最後の5行:")
    print(df.tail())
    
    # 日付フォーマットの確認
    print(f"\n日付フォーマットの例:")
    print(f"最初の日付: {df['date_time_jst'].iloc[0]}")
    print(f"最後の日付: {df['date_time_jst'].iloc[-1]}")
    
    # ファイルを保存
    df.to_csv(output_file, index=False, encoding='utf-8')
    print(f"\nファイルを保存しました: {output_file}")

if __name__ == "__main__":
    # 入力ファイルと出力ファイルのパス
    input_file = "csv/20250703_MAEBASHI02_1755167606.661285.csv"
    output_file = "csv/converted_data.csv"
    
    # 変換実行
    convert_csv_format(input_file, output_file)
