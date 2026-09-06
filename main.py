import glob
import os
import logging
import traceback
import sys

import mysql.connector
from datetime import datetime as dt, timedelta, timezone
import csv
from google.cloud import storage
from google.cloud import run_v2
import google.auth.transport.requests
import google.oauth2.id_token
from urllib.parse import urlparse

#from api.utils import preproc
#from api.model import EmsembleModel
#import api.config as config
import json

# apiディレクトリをパスに追加
base_dir = os.path.dirname(os.path.abspath(__file__))
api_dir = os.path.join(base_dir, 'api')
sys.path.insert(0, api_dir)

# EGPF 連携共通モジュール（リトライ・打ち切り・通知・失敗分類）
from egpf_common import (
    egpf_get, EgpfRetryExhausted, ConsecutiveFailureGuard, notify_error,
    classify_failure, describe_failure, format_task_summary,
    TITLE_TASK_FAILED, TITLE_TASK_ABORTED, TITLE_UNEXPECTED, ACTION_RECREATE_TASK,
)

# 通知の先頭に付ける機能名（例: [MCI Ver4][本番] 月次予測 失敗あり）
NOTIFY_SOURCE = "MCI Ver4"

# PredictorWithLoggingをインポート
try:
    from pred_mci import PredictorWithLogging
except (TypeError, ModuleNotFoundError) as e:
    # Python 3.8以前でtuple[...]型ヒントが使えない場合のエラーハンドリング
    if isinstance(e, TypeError) and "'type' object is not subscriptable" in str(e):
        import importlib.util
        import re

        api_path = os.path.join(api_dir, 'pred_mci.py')
        if not os.path.exists(api_path):
            raise FileNotFoundError(f"pred_mci.py not found: {api_path}")

        with open(api_path, 'r', encoding='utf-8') as f:
            source = f.read()

        source_fixed = re.sub(r'->\s*tuple\[', '-> Tuple[', source)
        source_fixed = re.sub(r':\s*tuple\[', ': Tuple[', source_fixed)

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

        from myexception import InvalidInputError, PredictionError, PredictionTimeOut, UnexpectedError, TIMEOUT, timeout_handler

        namespace = {
            'os': _os, 'pd': _pd, 'np': _np, 'pickle': _pickle, 'glob': _glob,
            'datetime': _datetime, 'lgb': _lgb, 'signal': _signal, 'wraps': _wraps,
            'time': _time, 'logging': _logging, 'RotatingFileHandler': _RotatingFileHandler,
            'Union': Union, 'List': List, 'Dict': Dict, 'Callable': Callable,
            'Any': Any, 'Tuple': _Tuple,
            'InvalidInputError': InvalidInputError, 'PredictionError': PredictionError,
            'PredictionTimeOut': PredictionTimeOut, 'UnexpectedError': UnexpectedError,
            'TIMEOUT': TIMEOUT, 'timeout_handler': timeout_handler,
        }

        compiled = compile(source_fixed, api_path, 'exec')
        exec(compiled, namespace)

        PredictorWithLogging = namespace['PredictorWithLogging']
    else:
        raise

# PredictorWithLoggingインスタンスをグローバルで1度だけ初期化
_predictor_instance = None

def get_predictor():
    """PredictorWithLoggingのシングルトンインスタンスを取得"""
    global _predictor_instance
    if _predictor_instance is None:
        logger = logging.getLogger(__name__)
        logger.info("PredictorWithLoggingクラスを初期化中...")
        _predictor_instance = PredictorWithLogging(
            lgb_models_dir_path=os.path.join(base_dir, "api", "models", "lgb", "*.txt"),
            logi_models_dir_path=os.path.join(base_dir, "api", "models", "logistic", "*.pkl"),
            lgb_scaler_path=os.path.join(base_dir, "api", "scaler", "lgb_scaler.pickle"),
            logi_scaler_path=os.path.join(base_dir, "api", "scaler", "logi_scaler.pickle")
        )
        logger.info("初期化完了")
    return _predictor_instance

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

def is_another_execution_running():
    """他に実行中のCloud Run Job実行があるかチェック"""
    logger = logging.getLogger(__name__)
    project_id = os.environ.get('GOOGLE_CLOUD_PROJECT')
    location = os.environ.get('CLOUD_RUN_REGION', 'asia-northeast1')
    job_name = os.environ.get('CLOUD_RUN_JOB')
    current_execution = os.environ.get('CLOUD_RUN_EXECUTION')

    logger.debug(f"実行中チェック: project_id={project_id}, location={location}, job_name={job_name}, current_execution={current_execution}")

    # 必要な環境変数が設定されていない場合（ローカル実行など）はチェックをスキップ
    if not all([project_id, job_name, current_execution]):
        logger.warning(f"実行中チェックをスキップ: 環境変数が不足 (project_id={project_id}, job_name={job_name}, current_execution={current_execution})")
        return False

    try:
        client = run_v2.ExecutionsClient()
        parent = f"projects/{project_id}/locations/{location}/jobs/{job_name}"
        logger.debug(f"実行一覧を取得中: parent={parent}")

        for execution in client.list_executions(parent=parent):
            logger.debug(f"実行を確認: name={execution.name}, reconciling={execution.reconciling}, running={execution.running_count}, succeeded={execution.succeeded_count}, failed={execution.failed_count}")

            # 自分自身はスキップ
            if current_execution in execution.name:
                logger.debug(f"自分自身をスキップ: {execution.name}")
                continue

            # 実行中かどうかチェック（reconciling=準備中も含む）
            if execution.reconciling or (
                execution.running_count > 0 and
                execution.succeeded_count == 0 and
                execution.failed_count == 0
            ):
                logger.info(f"他に実行中のジョブを検出: {execution.name}")
                return True

        logger.debug("他に実行中のジョブなし")
        return False
    except Exception as e:
        # APIエラーの場合は警告を出して継続（安全側に倒す）
        logger.warning(f"実行中チェックでエラーが発生しました: {e}")
        return False

def configure_logging():
    """ルートロガーのハンドラを組み直す。

    api/pred_mci.py が import 時に logging.basicConfig(..., StreamHandler()) を実行するため、
    従来は main() の basicConfig が無効化されて LOG_LEVEL が効かず、さらに全行が stderr に出て
    Cloud Logging では severity=ERROR 扱いになっていた。
    predictor.log への RotatingFileHandler は GCS 退避に使うので残し、それ以外のハンドラを
    標準出力（ERROR 以上は標準エラー出力）に付け替える。
    """
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    root = logging.getLogger()

    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            continue  # predictor.log（RotatingFileHandler）は維持
        root.removeHandler(handler)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(lambda record: record.levelno < logging.ERROR)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(logging.ERROR)
    root.addHandler(stdout_handler)
    root.addHandler(stderr_handler)
    root.setLevel(log_level)


def main():
    configure_logging()
    logger = logging.getLogger(__name__)

    logger.info("Start main.")

    # 他のジョブ実行が実行中かチェック
    if is_another_execution_running():
        logger.info("別のCloud Run Jobが実行中のため、このジョブをスキップします")
        sys.exit(0)

    cnx = None
    should_upload_log = False  # タスク処理が行われた場合のみログをアップロード
    # API URL
    api_url = os.environ.get('ENERGY_GATEWAY_API_URL', "https://api.energy-gateway.jp/0.2/estimated_data")
    # モックAPI URL（spid=9991の場合のみ使用）
    mock_api_url = os.environ.get('MOCK_API_URL')
    # エラー通知（ERROR_NOTIFY_SLACK_WEBHOOK_URL 未設定なら notify_error は何もしない）
    env_label = os.environ.get('ERROR_NOTIFY_ENV_LABEL')
    jst = timezone(timedelta(hours=+9))
    pending_notifications = []  # (title, summary): N1/N2 はログの GCS アップロード後にパスを添えて送る
    csv_header = ['date_time_jst', 'air_conditioner', 'clothes_washer', 'microwave', 'refrigerator', 'rice_cooker',
                  'TV', 'cleaner', 'IH', 'Heater']
    app_type_ids = [2, 5, 20, 24, 25, 30, 31, 37, 301]

    try:
        # Cloud SQL Proxy uses Unix socket, otherwise use host
        mysql_host = os.environ.get('MCI_MYSQL_HOST')
        connection_params = {
            'user': os.environ.get('MCI_MYSQL_USER'),
            'password': os.environ.get('MCI_MYSQL_PASSWORD'),
            'database': os.environ.get('MCI_MYSQL_DATABASE'),
            'time_zone': os.environ.get('MCI_MYSQL_TIMEZONE', "Asia/Tokyo"),
        }

        if mysql_host and mysql_host.startswith('/cloudsql/'):
            # Use unix_socket for Cloud SQL Proxy
            connection_params['unix_socket'] = mysql_host
        else:
            # Use host for direct connection
            connection_params['host'] = mysql_host

        cnx = mysql.connector.connect(**connection_params)

        if cnx.is_connected:
            logger.debug("Connected Mysql!")

        cursor = cnx.cursor()

        sql = "SELECT id AS task_id, date_from, date_to from `tasks` " \
              "WHERE start_at IS NULL AND starting_at < NOW() AND algorithm = %s " \
              "ORDER BY starting_at "
        param = (4,)
        cursor.execute(sql, param)

        tasks = cursor.fetchall()

        if len(tasks) == 0:
            logger.info("Exit because there are no tasks.")
            exit(0)

        try:
            pathname = f"/tmp/data/*.csv"
            for p in glob.glob(pathname):
                if p == f"/tmp/data/sample.csv":
                    continue
                if os.path.isfile(p):
                    os.remove(p)
        except (Exception,) as e:
            logger.warning("Warning Occurred. failed old csv files: exception: %s", e)

        for (task_id, date_from, date_to) in tasks:
            should_upload_log = True  # タスク処理開始
            task_started_at = dt.now(jst)
            failures = []  # (houseid, 失敗分類, 詳細)
            succeeded = 0
            remaining = None  # EGPF 障害の疑いで打ち切った場合の未処理ハウス数
            guard = ConsecutiveFailureGuard()
            try:
                # タスク毎
                logger.debug("Start task. task_id: %s", task_id)

                # task開始をDBに登録
                sql = "UPDATE `tasks` SET start_at=NOW() WHERE id = %s "
                param = (task_id,)
                cursor.execute(sql, param)
                cnx.commit()

                sql = "SELECT id AS task_house_id, spid, houseid, age, sex, education, solo from `task_houses` WHERE task_id = %s ORDER BY spid, id"
                param = (task_id,)
                cursor.execute(sql, param)
                task_houses = cursor.fetchall()

                logger.debug("get task_houses. count: %s", len(task_houses))

                for house_index, (task_house_id, spid, houseid, age, sex, education, solo) in enumerate(task_houses):
                    status = 0
                    progress = 0
                    try:
                        # ハウス毎
                        logger.debug("task_house. task_house_id: %s", task_house_id)

                        progress = 10
                        update_task_houses(cnx, cursor, task_house_id, status, progress)

                        # csv登録用
                        arr = []

                        # API取得
                        start = dt.strptime(f"{date_from} 00:00:00+0900", '%Y-%m-%d %H:%M:%S%z')
                        end = dt.strptime(f"{date_to} 00:00:00+0900", '%Y-%m-%d %H:%M:%S%z')
                        end = end + timedelta(days=1)
                        sub = end - start
                        exist_all = False
                        for day in range(sub.days):
                            sts = start + timedelta(days=day)
                            ets = start + timedelta(days=(day + 1))
                            # print(sts, ets)

                            headers = {'Authorization': f"imSP {spid}:{os.environ.get('API_SHARED_PASSWORD')}"}
                            params = {'service_provider': spid, 'house': houseid, 'sts': int(sts.timestamp()),
                                      'ets': int(ets.timestamp()), 'time_units': 20}

                            # spid=9991かつMOCK_API_URLが定義されている場合はモックサーバーを使用
                            if str(spid) == '9991' and mock_api_url:
                                url = mock_api_url
                                # mock-api-stgがIAM(run.invoker)保護されている場合のID tokenをX-Serverless-Authorizationで付与
                                # (Authorizationは既にEGCloud独自認証で使用済みのため別ヘッダ)
                                try:
                                    parsed = urlparse(mock_api_url)
                                    audience = f"{parsed.scheme}://{parsed.netloc}"
                                    id_tok = google.oauth2.id_token.fetch_id_token(
                                        google.auth.transport.requests.Request(), audience
                                    )
                                    headers['X-Serverless-Authorization'] = f'Bearer {id_tok}'
                                except Exception as e:
                                    logger.warning(f"mock-api-stg向けID token取得失敗(認証なしで継続): {e}")
                            else:
                                url = api_url

                            # リトライ付き GET（設計書 3 章）。2xx 以外は egpf_get が例外を送出する
                            res = egpf_get(url, headers, params,
                                           context={'spid': spid, 'house': houseid,
                                                    'sts': params['sts'], 'ets': params['ets']},
                                           logger=logger)
                            guard.record_success()
                            # print(r.json()['data'][0]['timestamps'])

                            response_data = res.json()
                            timestamps = response_data['data'][0]['timestamps']
                            appliance_types = response_data['data'][0]['appliance_types']

                            for i, timestamp in enumerate(timestamps):
                                date_time_jst = dt.fromtimestamp(timestamp).astimezone(
                                    timezone(timedelta(hours=+9))).strftime('%Y/%m/%d %H:%M:00')
                                # print(date_time_jst)
                                # print(appliance_types)

                                line = [
                                    None,
                                    None,
                                    None,
                                    None,
                                    None,
                                    None,
                                    None,
                                    None,
                                    None
                                ]

                                exist = False
                                for appliance_type in appliance_types:
                                    try:
                                        # print(appliance_type['appliance_type_id'])
                                        j = app_type_ids.index(int(appliance_type['appliance_type_id']))
                                        # print(appliance_type)
                                        # print(j)
                                        if j >= 0:
                                            # print('hit!')
                                            # print(appliance_type['appliances'][0]['powers'][i])
                                            if appliance_type['appliances'][0]['powers'][i] is not None:
                                                if float(appliance_type['appliances'][0]['powers'][i]) > 0.0:
                                                    flag = 1
                                                else:
                                                    flag = 0
                                                line[j] = flag
                                                exist = True
                                        else:
                                            continue
                                    except (Exception,):
                                        continue

                                # 1つでもnullでなければ0を代入
                                if exist:
                                    exist_all = True
                                    for k, row in enumerate(line):
                                        if row is None:
                                            line[k] = 0

                                arr.append(
                                    [date_time_jst,
                                     line[0], line[1], line[2], line[3], line[4], line[5], line[6], line[7], line[8]])

                        # print(arr)
                        progress = 20
                        update_task_houses(cnx, cursor, task_house_id, status, progress)

                        # 前欠損エラー
                        if (len(arr) == 0) or (exist_all is False):
                            raise ValueError("Total loss error!")

                        ts = dt.timestamp(dt.now())
                        csv_filename = f"{start.strftime('%Y%m%d')}_{houseid}_{int(ts)}.csv"
                        data_path = f"/tmp/data/{csv_filename}"  # input csv path
                        # CSV出力
                        with open(data_path, 'w') as f:
                            writer = csv.writer(f)
                            writer.writerow(csv_header)
                            writer.writerows(arr)

                        if (len(arr) == 0) or (exist_all is False):
                            raise ValueError("Total loss error!")

                        # CSVファイルをCloud Storageにバックアップ
                        gcs_bucket_name = os.environ.get('GCS_LOG_BUCKET')
                        if gcs_bucket_name:
                            try:
                                storage_client = storage.Client()
                                bucket = storage_client.bucket(gcs_bucket_name)
                                gcs_csv_path = f"data/{csv_filename}"
                                blob = bucket.blob(gcs_csv_path)
                                blob.upload_from_filename(data_path)
                                logger.debug(f"CSV file uploaded to gs://{gcs_bucket_name}/{gcs_csv_path}")
                            except Exception as e:
                                logger.warning(f"Failed to upload CSV to GCS: {e}")

                        progress = 30
                        update_task_houses(cnx, cursor, task_house_id, status, progress)

                        # data_path = f"/tmp/data/sample.csv"
                        # MySQLから取得した値を明示的にint型に変換
                        age_int = int(age) if age is not None else 0
                        sex_int = int(sex) if sex is not None else 0
                        edu_int = int(education) if education is not None else 0
                        solo_int = int(solo) if solo is not None else 0

                        args = Args(age_int, sex_int, edu_int, solo_int, data_path)

                        result = api_main(args)
                        logger.debug(f"api_main args: %s", json.dumps(vars(args)))

                        progress = 50
                        update_task_houses(cnx, cursor, task_house_id, status, progress)

                        # 結果をDBに登録 (int)($float * 100.0 + 0.5);
                        sql = "INSERT `task_results` (task_id, task_house_id, result, created_at) " \
                              "value (%s, %s, %s, NOW())"
                        param = (task_id, task_house_id, 100 - int(result))
                        cursor.execute(sql, param)
                        cnx.commit()

                        logger.debug(f"predicted. result: %s", result)

                        status = 1
                        progress = 100
                        update_task_houses(cnx, cursor, task_house_id, status, progress)
                        succeeded += 1

                    except Exception as e:
                        print(traceback.format_exc())
                        # ハウス毎のエラー（分類は設計書 4.5。ログ・通知にのみ使い DB には書かない）
                        category = classify_failure(e)
                        failures.append((houseid, category, describe_failure(e)))
                        egpf_exhausted = isinstance(e, EgpfRetryExhausted)
                        logger.warning(f"Warning Occurred. failed task_house. houseid: %s, category: %s, exception: %s",
                                       houseid, category, e)
                        try:
                            status = -1
                            update_task_houses(cnx, cursor, task_house_id, status, progress)

                            sql = "INSERT `task_results` (task_id, task_house_id, result, created_at) value (%s, %s, %s, " \
                                  "NOW()) "
                            param = (task_id, task_house_id, -1,)
                            cursor.execute(sql, param)
                            cnx.commit()
                        except Exception as e:
                            logger.warning(f"Warning Occurred. failed update_task_houses. exception: %s", e)
                        # EGPF 全断の疑い（設計書 3.5）: 連続でリトライ枯渇したらタスクを打ち切る。
                        # 処理中のハウスは上で -1 確定済み。残りのハウスは未処理のまま（task_houses を触らない）
                        if egpf_exhausted and guard.record_exhausted():
                            remaining = len(task_houses) - house_index - 1
                            break
                        continue

                summary = dict(task_id=task_id, started_at=task_started_at, ended_at=dt.now(jst),
                               total=len(task_houses), succeeded=succeeded, failures=failures)

                if remaining is not None:
                    # 打ち切り: tasks を失敗にして N2 通知。以降のタスクも処理しない
                    logger.error("Aborting task. task_id: %s, consecutive EGPF retry exhaustion. remaining houses: %s",
                                 task_id, remaining)
                    sql = "UPDATE `tasks` SET end_at=NOW(), status=%s WHERE id = %s"
                    param = (-1, task_id,)
                    cursor.execute(sql, param)
                    cnx.commit()
                    summary['remaining'] = remaining
                    pending_notifications.append((TITLE_TASK_ABORTED, summary))
                    break

                # task終了をDBに登録
                sql = "UPDATE `tasks` SET end_at=NOW(), status=%s WHERE id = %s"
                param = (1, task_id,)
                cursor.execute(sql, param)
                cnx.commit()

                # N1: 失敗ハウスがあればタスク単位で通知する（0 件なら通知しない）
                if failures:
                    pending_notifications.append((TITLE_TASK_FAILED, summary))

            except (Exception,) as e:
                # タスク毎のエラー
                logger.warning("Warning Occurred. failed task: exception: %s", e)

                sql = "UPDATE `tasks` SET end_at=NOW(), status=%s WHERE id = %s"
                param = (-1, task_id,)
                cursor.execute(sql, param)
                cnx.commit()
                # N3: タスク単位の例外
                notify_error(TITLE_UNEXPECTED, [f"task_id: {task_id}", f"例外: {describe_failure(e)}"],
                             env_label=env_label, logger=logger, source=NOTIFY_SOURCE)
                break

            logger.debug(f"Completed task. task_id: %s", task_id)

        # predictor.logをCloud Storageにアップロード
        log_path = upload_log_to_gcs(task_id)
        should_upload_log = False  # finally での二重アップロードを防ぐ

        # N1/N2: ログのパスを添えてタスク単位の通知を送る
        for (title, summary) in pending_notifications:
            lines = format_task_summary(log_path=log_path, action=ACTION_RECREATE_TASK, **summary)
            notify_error(title, lines, env_label=env_label, logger=logger, source=NOTIFY_SOURCE)
        pending_notifications = []

        logger.info("Completed main.")

    except (Exception,) as e:
        logger.error("Error Occurred. exception: %s", e)
        # N3: 最外殻の例外（DB 接続失敗等）。通知後は従来どおり exit(1)
        notify_error(TITLE_UNEXPECTED, [f"例外: {describe_failure(e)}", "処理を中断しました（exit 1）"],
                     env_label=env_label, logger=logger, source=NOTIFY_SOURCE)
        exit(1)
    finally:
        # タスク処理が行われた場合のみログをアップロード
        if should_upload_log:
            upload_log_to_gcs()
        if cnx is not None and cnx.is_connected():
            cnx.close()
            logger.debug("Closed Mysql!")


def update_task_houses(cnx, cursor, p_task_house_id, p_status, p_progress):
    m_sql = "UPDATE `task_houses` SET status = %s, progress = %s, updated_at = NOW() WHERE id = %s"
    m_param = (p_status, p_progress, p_task_house_id,)
    cursor.execute(m_sql, m_param)
    cnx.commit()


def upload_log_to_gcs(task_id=None):
    """predictor.logをCloud Storageにアップロード（エラー時も実行）

    戻り値: 退避先のパス（gs://... またはローカルの保存先）。退避できなかった場合は None
    """
    logger = logging.getLogger(__name__)

    # pred_mci.pyはカレントディレクトリにログを作成するため、両方の場所を確認
    possible_paths = [
        os.path.join(base_dir, 'predictor.log'),  # base_dir
        'predictor.log',  # カレントディレクトリ
        os.path.join(os.getcwd(), 'predictor.log'),  # カレントディレクトリ（絶対パス）
    ]

    predictor_log_path = None
    for path in possible_paths:
        if os.path.exists(path):
            predictor_log_path = path
            break

    if not predictor_log_path:
        return None

    try:
        gcs_bucket_name = os.environ.get('GCS_LOG_BUCKET')
        task_id_str = str(task_id).zfill(10) if task_id else 'unknown'

        if gcs_bucket_name:
            # Cloud Storageにアップロード
            storage_client = storage.Client()
            bucket = storage_client.bucket(gcs_bucket_name)
            log_filename = f"logs/predictor_{task_id_str}_{dt.now().strftime('%Y%m%d%H%M%S')}.log"
            blob = bucket.blob(log_filename)
            blob.upload_from_filename(predictor_log_path)
            logger.info(f"Log file uploaded to gs://{gcs_bucket_name}/{log_filename}")
            # アップロード後、ローカルファイルを削除
            os.remove(predictor_log_path)
            return f"gs://{gcs_bucket_name}/{log_filename}"
        else:
            # GCS_LOG_BUCKETが設定されていない場合は、従来通りローカルに保存
            logger.warning("GCS_LOG_BUCKET is not set. Saving log locally.")
            os.makedirs('log', exist_ok=True)
            log_filename = f"predictor_{task_id_str}_{dt.now().strftime('%Y%m%d%H%M%S')}.log"
            new_log_path = os.path.join(base_dir, 'log', log_filename)
            os.rename(predictor_log_path, new_log_path)
            return new_log_path
    except Exception as e:
        logger.warning(f"Failed to upload log to GCS: {e}. Saving locally.")
        try:
            os.makedirs('log', exist_ok=True)
            task_id_str = str(task_id).zfill(10) if task_id else 'unknown'
            log_filename = f"predictor_{task_id_str}_{dt.now().strftime('%Y%m%d%H%M%S')}.log"
            new_log_path = os.path.join(base_dir, 'log', log_filename)
            os.rename(predictor_log_path, new_log_path)
            return new_log_path
        except Exception:
            pass
    return None


def api_main(args):
    logger = logging.getLogger(__name__)
    age = args.age
    male = args.male
    edu = args.edu
    solo = args.solo
    csv_path = args.csv
    debug = False

    try:
        # シングルトンインスタンスを取得（初回のみ初期化される）
        predictor = get_predictor()

        # 予測実行
        logger.info("予測を実行中...")
        logger.info(f"age: {age}, male: {male}, edu: {edu}, solo: {solo}, csv_path: {csv_path}")
        result = predictor.calculate_score(age, male, edu, solo, csv_path, debug=debug)

        status_code = result.get('status_code')
        if status_code == 100:
            return result.get('score')
        else:
            logger.error(f"エラーが発生しました。ステータスコード: {status_code}")
            raise Exception(get_status_message(status_code))
    except Exception as e:
        logger.error(f"予測実行中にエラーが発生しました: {e}")
        logger.error(traceback.format_exc())
        raise

class Args:
    def __init__(self, age, male, edu, solo, data_path):
        if male != 1:
            male = 0  # 女性は0
        if solo is None:
            solo = 0  # Noneの場合は0
        self.age = age
        self.male = male
        self.edu = edu
        self.solo = solo
        self.csv = data_path


if __name__ == '__main__':
    main()
