#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCI予測APIを使用するメインスクリプト

このスクリプトは、api/pred_mciモジュールのPredictorクラスを使用して
MCI（軽度認知障害）予測を実行します。
"""
from __future__ import annotations

import os
import sys
import logging
import traceback
import glob
import csv
import argparse
from typing import Dict, Any, Optional
from datetime import datetime as dt, timedelta, timezone

import mysql.connector
import requests

# ロギング設定（早期に設定）
# 環境変数LOG_LEVELを参照（Docker環境対応）
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# Docker環境対応: 作業ディレクトリを取得
# docker-compose.ymlではworking_dir: /tmpが設定されている
base_dir = os.path.dirname(os.path.abspath(__file__))
api_dir = os.path.join(base_dir, 'api')

# apiディレクトリをパスに追加
sys.path.insert(0, api_dir)

logger.debug(f"作業ディレクトリ: {base_dir}")
logger.debug(f"APIディレクトリ: {api_dir}")

try:
    from pred_mci import Predictor
except (TypeError, ModuleNotFoundError) as e:
    # Python 3.8以前でtuple[...]型ヒントが使えない場合のエラーハンドリング
    if isinstance(e, TypeError) and "'type' object is not subscriptable" in str(e):
        import importlib.util
        import re

        # 動的にモジュールを読み込む（型ヒントを修正）
        # Docker環境対応: 絶対パスを使用
        api_path = os.path.join(api_dir, 'pred_mci.py')

        # ファイルが存在するか確認
        if not os.path.exists(api_path):
            logger.error(f"pred_mci.pyが見つかりません: {api_path}")
            raise FileNotFoundError(f"pred_mci.py not found: {api_path}")

        # 型ヒントの構文エラーを回避するため、モジュールのソースを修正して読み込む
        with open(api_path, 'r', encoding='utf-8') as f:
            source = f.read()

        # tuple[...]をTuple[...]に一時的に置き換え（メモリ上のみ）
        source_fixed = re.sub(r'->\s*tuple\[', '-> Tuple[', source)
        source_fixed = re.sub(r':\s*tuple\[', ': Tuple[', source_fixed)

        # 必要な依存関係をインポート
        import os as _os
        import pandas as _pd
        import numpy as _np
        import pickle as _pickle
        import glob as _glob
        import datetime as _datetime
        import lightgbm as _lgb
        import signal as _signal
        from functools import wraps as _wraps
        import time as _time
        import logging as _logging
        from logging.handlers import RotatingFileHandler as _RotatingFileHandler
        from typing import Union, List, Dict, Callable, Any, Tuple as _Tuple

        # apiディレクトリ内のmyexceptionモジュールをインポート
        # 既にsys.pathに追加済みなので、そのままインポート
        try:
            from myexception import InvalidInputError, PredictionError, PredictionTimeOut, UnexpectedError, TIMEOUT, timeout_handler
        except ImportError as ie:
            logger.error(f"myexceptionモジュールのインポートに失敗しました: {ie}")
            logger.error(f"sys.path: {sys.path}")
            logger.error(f"api_dir: {api_dir}")
            raise

        # 名前空間を準備（pred_mci.pyが使用するすべてのモジュールを含む）
        namespace = {
            'os': _os,
            'pd': _pd,
            'np': _np,
            'pickle': _pickle,
            'glob': _glob,
            'datetime': _datetime,
            'lgb': _lgb,
            'signal': _signal,
            'wraps': _wraps,
            'time': _time,
            'logging': _logging,
            'RotatingFileHandler': _RotatingFileHandler,
            'Union': Union,
            'List': List,
            'Dict': Dict,
            'Callable': Callable,
            'Any': Any,
            'Tuple': _Tuple,
            'InvalidInputError': InvalidInputError,
            'PredictionError': PredictionError,
            'PredictionTimeOut': PredictionTimeOut,
            'UnexpectedError': UnexpectedError,
            'TIMEOUT': TIMEOUT,
            'timeout_handler': timeout_handler,
            '__file__': api_path,  # モジュールのファイルパスを設定
            '__name__': 'pred_mci',  # モジュール名を設定
        }

        # モジュールをコンパイルして実行
        compiled = compile(source_fixed, api_path, 'exec')
        exec(compiled, namespace)

        # Predictorクラスを取得
        if 'Predictor' not in namespace:
            raise ImportError("Predictorクラスが見つかりません")
        Predictor = namespace['Predictor']
    else:
        # その他のエラー（ModuleNotFoundErrorなど）
        logger.error(f"pred_mciモジュールのインポートに失敗しました: {e}")
        logger.error(f"エラータイプ: {type(e).__name__}")
        logger.error(f"sys.path: {sys.path}")
        logger.error(f"api_dir: {api_dir}")
        logger.error(f"api_dirの存在: {os.path.exists(api_dir)}")
        traceback.print_exc()
        raise
except Exception as e:
    # その他のインポートエラー
    logger.error(f"Predictorクラスのインポートに失敗しました: {e}")
    logger.error(f"エラータイプ: {type(e).__name__}")
    traceback.print_exc()
    raise


def get_status_message(status_code: int) -> str:
    """
    ステータスコードに対応するメッセージを取得

    :param status_code: ステータスコード
    :return: ステータスメッセージ
    """
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
    return status_messages.get(status_code, "不明なステータスコード")


# def interpret_score(score: int) -> str:
#     """
#     スコアから判定結果を返す

#     :param score: 認知機能スコア（0-100）
#     :return: 判定結果
#     """
#     if score >= 80:
#         return "正常"
#     elif score >= 60:
#         return "軽度認知障害の可能性"
#     else:
#         return "認知症の可能性"


def predict_mci(
    age: int,
    male: int,
    edu: int,
    solo: int,
    csv_path: str,
    debug: bool = False
) -> Dict[str, Any]:
    """
    MCI予測を実行する

    :param age: 年齢
    :param male: 男性かどうか（男性=1、女性=0）
    :param edu: 教育年数
    :param solo: 独居かどうか（独居=1、同居者あり=0）
    :param csv_path: 電力データのCSVファイルパス（apiディレクトリからの相対パス）
    :param debug: デバッグモード（Trueの場合、詳細な予測値を返す）
    :return: 予測結果の辞書
    """
    try:
        # Predictorインスタンスの作成
        logger.info("Predictorクラスを初期化中...")
        # Docker環境対応: base_dirからの相対パスを使用
        predictor = Predictor(
            lgb_models_dir_path=os.path.join(base_dir, "api", "models", "lgb", "*.txt"),
            logi_models_dir_path=os.path.join(base_dir, "api", "models", "logistic", "*.pkl"),
            lgb_scaler_path=os.path.join(base_dir, "api", "scaler", "lgb_scaler.pickle"),
            logi_scaler_path=os.path.join(base_dir, "api", "scaler", "logi_scaler.pickle")
        )
        logger.info("初期化完了")

        # 予測実行
        logger.info("予測を実行中...")
        result = predictor.calculate_score(age, male, edu, solo, csv_path, debug=debug)

        return result

    except Exception as e:
        logger.error(f"予測実行中にエラーが発生しました: {e}")
        logger.error(traceback.format_exc())
        raise


def update_task_houses(cnx, cursor, task_house_id: str, status: int, progress: int):
    """
    task_housesテーブルの進捗を更新する

    :param cnx: MySQL接続オブジェクト
    :param cursor: MySQLカーソルオブジェクト
    :param task_house_id: タスクハウスID
    :param status: ステータス（0=処理中, 1=成功, -1=失敗）
    :param progress: 進捗（0-100）
    """
    sql = "UPDATE `task_houses` SET status = %s, progress = %s, updated_at = NOW() WHERE id = %s"
    param = (status, progress, task_house_id,)
    cursor.execute(sql, param)
    cnx.commit()


def fetch_electric_data_from_api(
    spid: str,
    houseid: str,
    date_from: str,
    date_to: str,
    api_url: str,
    api_password: str,
    app_type_ids: list
) -> tuple:
    """
    APIから電力データを取得してCSV形式の配列を返す

    :param spid: サービスプロバイダーID
    :param houseid: ハウスID
    :param date_from: 開始日（YYYY-MM-DD形式）
    :param date_to: 終了日（YYYY-MM-DD形式）
    :param api_url: APIエンドポイントURL
    :param api_password: API共有パスワード
    :param app_type_ids: 取得する家電タイプIDのリスト
    :return: CSVデータの配列
    """
    csv_header = ['date_time_jst', 'air_conditioner', 'clothes_washer', 'microwave', 'refrigerator', 'rice_cooker',
                  'TV', 'cleaner', 'IH', 'Heater']

    start = dt.strptime(f"{date_from} 00:00:00+0900", '%Y-%m-%d %H:%M:%S%z')
    end = dt.strptime(f"{date_to} 00:00:00+0900", '%Y-%m-%d %H:%M:%S%z')
    end = end + timedelta(days=1)
    sub = end - start

    arr = []
    exist_all = False

    for day in range(sub.days):
        sts = start + timedelta(days=day)
        ets = start + timedelta(days=(day + 1))

        headers = {'Authorization': f"imSP {spid}:{api_password}"}
        params = {
            'service_provider': spid,
            'house': houseid,
            'sts': int(sts.timestamp()),
            'ets': int(ets.timestamp()),
            'time_units': 20
        }

        try:
            res = requests.get(api_url, headers=headers, params=params)
            res.raise_for_status()

            timestamps = res.json()['data'][0]['timestamps']
            appliance_types = res.json()['data'][0]['appliance_types']

            for i, timestamp in enumerate(timestamps):
                date_time_jst = dt.fromtimestamp(timestamp).astimezone(
                    timezone(timedelta(hours=+9))).strftime('%Y-%m-%d %H:%M:00')

                line = [None] * 9

                exist = False
                for appliance_type in appliance_types:
                    try:
                        j = app_type_ids.index(int(appliance_type['appliance_type_id']))
                        if j >= 0:
                            if appliance_type['appliances'][0]['powers'][i] is not None:
                                if float(appliance_type['appliances'][0]['powers'][i]) > 0.0:
                                    flag = 1
                                else:
                                    flag = 0
                                line[j] = flag
                                exist = True
                    except (Exception,):
                        continue

                # 1つでもnullでなければ0を代入
                if exist:
                    exist_all = True
                    for k, row in enumerate(line):
                        if row is None:
                            line[k] = 0

                arr.append([date_time_jst] + line)

        except Exception as e:
            logger.warning(f"API取得エラー (day={day}): {e}")
            continue

    if (len(arr) == 0) or (exist_all is False):
        raise ValueError("Total loss error: 電力データが取得できませんでした")

    return arr, csv_header


# def run_prediction_with_db(
#     csv_path: str,
#     age: int,
#     male: int,
#     edu: int,
#     solo: int,
#     debug: bool = False,
#     task_id: Optional[int] = None,
#     task_house_id: Optional[str] = None
# ):
#     """
#     CSVファイルを直接指定して予測を実行し、DBにも結果を保存するモード

#     :param csv_path: CSVファイルパス
#     :param age: 年齢
#     :param male: 男性かどうか（男性=1、女性=0）
#     :param edu: 教育年数
#     :param solo: 独居かどうか（独居=1、同居者あり=0）
#     :param debug: デバッグモード
#     :param task_id: タスクID（DBに保存する場合に指定）
#     :param task_house_id: タスクハウスID（DBに保存する場合に指定）
#     """
#     logger.info("=== CSVファイルから直接予測実行モード（DB保存あり） ===")

#     # CSVファイルの存在確認
#     if not os.path.exists(csv_path):
#         logger.error(f"CSVファイルが見つかりません: {csv_path}")
#         sys.exit(1)

#     # 絶対パスに変換
#     if not os.path.isabs(csv_path):
#         csv_path = os.path.join(base_dir, csv_path)

#     logger.info(f"CSVファイル: {csv_path}")
#     logger.info(f"年齢: {age}歳")
#     logger.info(f"性別: {'男性' if male == 1 else '女性'}")
#     logger.info(f"教育年数: {edu}年")
#     logger.info(f"独居: {'はい' if solo == 1 else 'いいえ'}")
#     if task_id:
#         logger.info(f"タスクID: {task_id}")
#     if task_house_id:
#         logger.info(f"タスクハウスID: {task_house_id}")

#     cnx = None

#     try:
#         # predict_mci関数を使用して予測実行
#         result = predict_mci(age, male, edu, solo, csv_path, debug=debug)

#         # 結果の表示
#         print("\n=== 予測結果 ===")
#         status_code = result.get('status_code')
#         score = result.get('score')

#         print(f"ステータスコード: {status_code}")
#         print(f"ステータス: {get_status_message(status_code)}")

#         if status_code == 100:
#             if debug:
#                 if isinstance(score, dict):
#                     print("\n=== 詳細予測値（デバッグモード） ===")
#                     print(f"LightGBMモデル予測値: {score.get('lightgbm', 'N/A'):.4f}")
#                     print(f"Logistic回帰モデル予測値: {score.get('logistic', 'N/A'):.4f}")
#                     print(f"Soft Voting予測値: {score.get('soft_voting', 'N/A'):.4f}")
#                     soft_voting = score.get('soft_voting', 0)
#                     if isinstance(soft_voting, float):
#                         cognitive_score = int(soft_voting * 100)
#                         print(f"\n認知機能スコア: {cognitive_score}")
#                         print(f"判定: {interpret_score(cognitive_score)}")
#             else:
#                 if score is not None:
#                     print(f"認知機能スコア: {score}")
#                     print(f"判定: {interpret_score(score)}")
#         else:
#             print(f"\nエラーが発生しました。スコア: {score}")
#             logger.error(f"エラーが発生しました。スコア: {score}")

#         # DBに結果を保存（task_idとtask_house_idが指定されている場合）
#         if task_id and task_house_id:
#             try:
#                 logger.info("DBに結果を保存中...")
#                 cnx = mysql.connector.connect(
#                     user=os.environ.get('MCI_MYSQL_USER'),
#                     password=os.environ.get('MCI_MYSQL_PASSWORD'),
#                     host=os.environ.get('MCI_MYSQL_HOST'),
#                     database=os.environ.get('MCI_MYSQL_DATABASE'),
#                     time_zone=os.environ.get('MCI_MYSQL_TIMEZONE', "Asia/Tokyo"),
#                 )

#                 if cnx.is_connected:
#                     cursor = cnx.cursor()

#                     # 結果をDBに登録
#                     if status_code == 100 and score is not None:
#                         db_result = 100 - score
#                         logger.info(f"predicted. result: {score} -> db_result: {db_result}")
#                     else:
#                         db_result = -1
#                         logger.warning(f"予測エラー: status_code={status_code}, score={score}")

#                     sql = "INSERT `task_results` (task_id, task_house_id, result, created_at) " \
#                           "value (%s, %s, %s, NOW())"
#                     param = (task_id, 4, db_result,)
#                     cursor.execute(sql, param)
#                     cnx.commit()

#                     # task_housesテーブルの進捗を更新
#                     status = 1 if status_code == 100 and score is not None else -1
#                     progress = 100
#                     update_task_houses(cnx, cursor, task_house_id, status, progress)

#                     logger.info("DBへの保存が完了しました")

#             except Exception as db_error:
#                 logger.warning(f"DBへの保存に失敗しました: {db_error}")
#                 # DBエラーでも予測結果は表示されているので、処理は継続
#             finally:
#                 if cnx is not None and cnx.is_connected():
#                     cnx.close()
#         elif task_id or task_house_id:
#             logger.warning("task_idとtask_house_idの両方を指定してください。DBへの保存をスキップします。")

#         logger.info("=== 予測処理完了 ===")
#         return result

#     except Exception as e:
#         logger.error(f"予測処理中にエラーが発生しました: {e}")
#         traceback.print_exc()
#         sys.exit(1)


def main():
    """
    メイン処理
    main.pyを参考に、DBからタスクを取得し、APIからデータを取得して予測を実行し、結果をDBに保存する
    または、コマンドライン引数でCSVファイルが指定された場合は、直接予測のみを実行する
    """
    logger.info("Start main.")

    # コマンドライン引数の解析
    parser = argparse.ArgumentParser(
        description='MCI予測スクリプト。DBからタスクを取得して予測を実行するか、直接CSVファイルから予測を実行します。'
    )
    parser.add_argument('--csv', '-c', type=str, help='CSVファイルパス（指定した場合はAPI取得をスキップして直接予測を実行）')
    parser.add_argument('--age', type=int, default=70, help='年齢（デフォルト: 70）')
    parser.add_argument('--male', type=int, default=0, choices=[0, 1], help='性別（男性=1、女性=0、デフォルト: 0）')
    parser.add_argument('--edu', type=int, default=12, help='教育年数（デフォルト: 12）')
    parser.add_argument('--solo', type=int, default=1, choices=[0, 1], help='独居（独居=1、同居者あり=0、デフォルト: 1）')
    parser.add_argument('--debug', action='store_true', help='デバッグモードで実行')
    parser.add_argument('--task-id', type=int, help='タスクID（CSV直接実行時にDBに保存する場合に必要）')
    parser.add_argument('--task-house-id', type=str, help='タスクハウスID（CSV直接実行時にDBに保存する場合に必要）')

    args = parser.parse_args()

    # CSVファイルが指定された場合は、直接予測モードで実行（DB処理も含む）
    # if args.csv:
    #     run_prediction_with_db(
    #         args.csv,
    #         args.age,
    #         args.male,
    #         args.edu,
    #         args.solo,
    #         args.debug,
    #         args.task_id,
    #         args.task_house_id
    #     )
    #     return

    # 通常モード: DBからタスクを取得してAPIからデータを取得
    cnx = None
    url = "https://api.energy-gateway.jp/0.2/estimated_data"
    app_type_ids = [2, 5, 20, 24, 25, 30, 31, 37, 301]
    data_dir = os.path.join(base_dir, "data")

    # dataディレクトリが存在しない場合は作成
    os.makedirs(data_dir, exist_ok=True)

    try:
        # MySQL接続
        cnx = mysql.connector.connect(
            user=os.environ.get('MCI_MYSQL_USER'),
            password=os.environ.get('MCI_MYSQL_PASSWORD'),
            host=os.environ.get('MCI_MYSQL_HOST'),
            database=os.environ.get('MCI_MYSQL_DATABASE'),
            time_zone=os.environ.get('MCI_MYSQL_TIMEZONE', "Asia/Tokyo"),
        )

        if cnx.is_connected:
            logger.debug("Connected Mysql!")

        cursor = cnx.cursor()

        # 処理対象のタスクを取得
        sql = "SELECT id AS task_id, date_from, date_to from `tasks` " \
              "WHERE start_at IS NULL AND starting_at < NOW() AND algorithm = %s " \
              "ORDER BY starting_at "
        param = (4,)
        cursor.execute(sql, param)

        tasks = cursor.fetchall()

        if len(tasks) == 0:
            logger.info("Exit because there are no tasks.")
            return

        # 古いCSVファイルを削除
        try:
            logger.info(f"削除対象のCSVファイル: {data_dir}")
            pathname = os.path.join(data_dir, "*.csv")
            for p in glob.glob(pathname):
                if os.path.basename(p) == "sample.csv":
                    continue
                if os.path.isfile(p):
                    os.remove(p)
                    logger.debug(f"削除したCSVファイル: {p}")
        except Exception as e:
            logger.warning(f"Warning Occurred. failed old csv files: exception: {e}")

        # Predictorインスタンスはpredict_mci関数内で作成されるため、ここでは作成しない

        for (task_id, date_from, date_to) in tasks:
            try:
                # タスク毎
                logger.info(f"Start task. task_id: {task_id}")

                # task開始をDBに登録
                sql = "UPDATE `tasks` SET start_at=NOW() WHERE id = %s "
                param = (task_id,)
                cursor.execute(sql, param)
                cnx.commit()

                # タスクに紐づくハウス情報を取得
                sql = "SELECT id AS task_house_id, spid, houseid, age, sex, education, solo " \
                      "from `task_houses` WHERE task_id = %s ORDER BY spid, id"
                param = (task_id,)
                cursor.execute(sql, param)
                task_houses = cursor.fetchall()

                logger.debug(f"get task_houses. count: {len(task_houses)}")

                for (task_house_id, spid, houseid, age, sex, education, solo) in task_houses:
                    status = 0
                    progress = 0
                    try:
                        # ハウス毎
                        logger.debug(f"task_house. task_house_id: {task_house_id}")

                        progress = 10
                        update_task_houses(cnx, cursor, task_house_id, status, progress)

                        # APIから電力データを取得
                        logger.debug(f"APIから電力データを取得中... (spid={spid}, houseid={houseid})")
                        api_password = os.environ.get('API_SHARED_PASSWORD')
                        arr, csv_header = fetch_electric_data_from_api(
                            spid, houseid, date_from, date_to, url, api_password, app_type_ids
                        )

                        progress = 20
                        update_task_houses(cnx, cursor, task_house_id, status, progress)

                        # CSVファイルに保存
                        ts = dt.timestamp(dt.now())
                        start_date = dt.strptime(f"{date_from} 00:00:00+0900", '%Y-%m-%d %H:%M:%S%z')
                        data_path = os.path.join(data_dir, f"{start_date.strftime('%Y%m%d')}_{houseid}_{ts}.csv")

                        logger.debug(f"CSVファイルに保存中: {data_path}")
                        with open(data_path, 'w', encoding='utf-8', newline='') as f:
                            writer = csv.writer(f)
                            writer.writerow(csv_header)
                            writer.writerows(arr)

                        progress = 30
                        update_task_houses(cnx, cursor, task_house_id, status, progress)

                        # 性別と教育年数の変換
                        male = 1 if sex == 1 else 0

                        if isinstance(education, str):
                            if education == '小卒':
                                edu = 6
                            elif education == '中卒':
                                edu = 9
                            elif education == '高卒':
                                edu = 12
                            elif education == '大卒':
                                edu = 16
                            elif education == '院卒（修士）':
                                edu = 17
                            elif education == '院卒（博士）':
                                edu = 22
                            else:
                                edu = 9
                        else:
                            edu = int(education) if education else 9

                        # MCI予測を実行
                        logger.debug(f"予測実行中... (age={age}, male={male}, edu={edu}, solo={solo})")
                        result = predict_mci(age, male, edu, solo, data_path, debug=False)

                        progress = 50
                        update_task_houses(cnx, cursor, task_house_id, status, progress)

                        # 結果をDBに登録
                        status_code = result.get('status_code')
                        score = result.get('score')

                        logger.info(f"ステータスコード: {status_code}")
                        logger.info(f"ステータス: {get_status_message(status_code)}")

                        if status_code == 100 and score is not None:
                            # 予測成功: scoreは0-100の値なので、main.pyと同じ計算式で変換
                            # main.py: 100 - int(float(result) * 100.0 + 0.5)
                            # ここでは、scoreは既に0-100の整数なので、100-scoreで変換
                            db_result = 100 - score
                            logger.debug(f"predicted. result: {score} -> db_result: {db_result}")
                        else:
                            # エラーの場合
                            db_result = -1
                            logger.warning(f"予測エラー: status_code={status_code}, score={score}")

                        sql = "INSERT `task_results` (task_id, task_house_id, result, created_at) " \
                              "value (%s, %s, %s, NOW())"
                        param = (task_id, task_house_id, db_result,)
                        cursor.execute(sql, param)
                        cnx.commit()

                        status = 1
                        progress = 100
                        update_task_houses(cnx, cursor, task_house_id, status, progress)

                    except Exception as e:
                        print(traceback.format_exc())
                        # ハウス毎のエラー
                        logger.warning(f"Warning Occurred. failed task_house. exception: {e}")
                        try:
                            status = -1
                            update_task_houses(cnx, cursor, task_house_id, status, progress)

                            sql = "INSERT `task_results` (task_id, task_house_id, result, created_at) value (%s, %s, %s, " \
                                  "NOW()) "
                            param = (task_id, task_house_id, -1,)
                            cursor.execute(sql, param)
                            cnx.commit()
                        except Exception as e2:
                            logger.warning(f"Warning Occurred. failed update_task_houses. exception: {e2}")
                        continue

                # task終了をDBに登録
                sql = "UPDATE `tasks` SET end_at=NOW(), status=%s WHERE id = %s"
                param = (1, task_id,)
                cursor.execute(sql, param)
                cnx.commit()

                logger.info(f"Completed task. task_id: {task_id}")

            except Exception as e:
                # タスク毎のエラー
                logger.warning(f"Warning Occurred. failed task: exception: {e}")
                traceback.print_exc()

                sql = "UPDATE `tasks` SET end_at=NOW(), status=%s WHERE id = %s"
                param = (-1, task_id,)
                cursor.execute(sql, param)
                cnx.commit()
                break

        logger.info("Completed main.")

    except Exception as e:
        logger.error(f"Error Occurred. exception: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        if cnx is not None and cnx.is_connected():
            cnx.close()
            logger.debug("Closed Mysql!")


if __name__ == '__main__':
    main()

