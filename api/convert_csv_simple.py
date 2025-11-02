#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CSVファイルの日付フォーマットを変換するスクリプト（標準ライブラリ版）
期待される形式: YYYY/MM/DD HH:MM
"""

import csv
import datetime
import os

def convert_csv_format(input_file, output_file):
    """
    CSVファイルの日付フォーマットを変換する
    
    Args:
        input_file (str): 入力CSVファイルのパス
        output_file (str): 出力CSVファイルのパス
    """
    
    print("入力ファイル: {}".format(input_file))
    print("出力ファイル: {}".format(output_file))
    
    # 必要な列名
    required_columns = ['date_time_jst', 'air_conditioner', 'clothes_washer', 'microwave', 'rice_cooker', 'TV', 'cleaner', 'IH', 'Heater']
    
    with open(input_file, 'r', encoding='utf-8') as infile, open(output_file, 'w', encoding='utf-8', newline='') as outfile:
        reader = csv.DictReader(infile)
        
        # ヘッダー行を処理
        fieldnames = required_columns + ['dummy_column']  # 10列目として追加
        
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        
        row_count = 0
        for row in reader:
            row_count += 1
            
            # 日付フォーマットを変換
            try:
                # 元の日付形式: 2025-07-03 00:00:00
                original_date = row['date_time_jst']
                # datetimeオブジェクトに変換
                dt = datetime.datetime.strptime(original_date, '%Y-%m-%d %H:%M:%S')
                # 新しい形式に変換: 2025/07/03 00:00
                new_date = dt.strftime('%Y/%m/%d %H:%M')
                
                # 新しい行を作成
                new_row = {
                    'date_time_jst': new_date,
                    'air_conditioner': row['air_conditioner'],
                    'clothes_washer': row['clothes_washer'],
                    'microwave': row['microwave'],
                    'rice_cooker': row['rice_cooker'],
                    'TV': row['TV'],
                    'cleaner': row['cleaner'],
                    'IH': row['IH'],
                    'Heater': row['Heater'],
                    'dummy_column': '0'  # 10列目
                }
                
                writer.writerow(new_row)
                
                # 最初の5行と最後の5行を表示
                if row_count <= 5:
                    print("変換後の行 {}: {}".format(row_count, new_row))
                    
            except Exception as e:
                print("エラー: 行 {} の処理中にエラーが発生しました: {}".format(row_count, e))
                break
    
    print("変換完了: {} 行を処理しました".format(row_count))
    print("ファイルを保存しました: {}".format(output_file))

if __name__ == "__main__":
    # 入力ファイルと出力ファイルのパス
    input_file = "csv/20250703_MAEBASHI02_1755167606.661285.csv"
    output_file = "csv/converted_data.csv"
    
    # 変換実行
    convert_csv_format(input_file, output_file)

