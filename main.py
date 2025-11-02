import glob
import os
import logging
import traceback

import mysql.connector
import requests
from datetime import datetime as dt, timedelta, timezone
import csv

#from api.utils import preproc
#from api.model import EmsembleModel
#import api.config as config
import json


def main():
    logger = logging.getLogger(__name__)
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s %(levelname)s %(message)s')

    logger.info("Start main.")

    cnx = None
    url = "https://api.energy-gateway.jp/0.2/estimated_data"
    csv_header = ['date_time_jst', 'air_conditioner', 'clothes_washer', 'microwave', 'refrigerator', 'rice_cooker',
                  'TV', 'cleaner', 'IH', 'Heater']
    app_type_ids = [2, 5, 20, 24, 25, 30, 31, 37, 301]

    try:
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

                for (task_house_id, spid, houseid, age, sex, education, solo) in task_houses:
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
                        start = dt.strptime(f"{date_from} 00:00:00+0900", '%Y/%m/%d %H:%M:%S%z')
                        end = dt.strptime(f"{date_to} 00:00:00+0900", '%Y/%m/%d %H:%M:%S%z')
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
                            # print(headers, params)

                            res = requests.get(url, headers=headers, params=params)
                            # print(r.json()['data'][0]['timestamps'])

                            timestamps = res.json()['data'][0]['timestamps']
                            appliance_types = res.json()['data'][0]['appliance_types']

                            for i, timestamp in enumerate(timestamps):
                                date_time_jst = dt.fromtimestamp(timestamp).astimezone(
                                    timezone(timedelta(hours=+9))).strftime('%Y-%m-%d %H:%M:00')
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
                        data_path = f"/tmp/data/{start.strftime('%Y%m%d')}_{houseid}_{ts}.csv"  # input csv path
                        # CSV出力
                        with open(data_path, 'w') as f:
                            writer = csv.writer(f)
                            writer.writerow(csv_header)
                            writer.writerows(arr)

                        if (len(arr) == 0) or (exist_all is False):
                            raise ValueError("Total loss error!")

                        progress = 30
                        update_task_houses(cnx, cursor, task_house_id, status, progress)

                        # data_path = f"/tmp/data/sample.csv"
                        args = Args(age, sex, education, solo, data_path)

                        result = api_main(args)
                        logger.debug(f"api_main args: %s", json.dumps(vars(args)))

                        progress = 50
                        update_task_houses(cnx, cursor, task_house_id, status, progress)

                        # 結果をDBに登録 (int)($float * 100.0 + 0.5);
                        sql = "INSERT `task_results` (task_id, task_house_id, result, created_at) " \
                              "value (%s, %s, %s, NOW())"
                        param = (task_id, task_house_id, 100 - int(float(result) * 100.0 + 0.5),)
                        cursor.execute(sql, param)
                        cnx.commit()

                        logger.debug(f"predicted. result: %s", result)

                        status = 1
                        progress = 100
                        update_task_houses(cnx, cursor, task_house_id, status, progress)

                    except Exception as e:
                        print(traceback.format_exc())
                        # ハウス毎のエラー
                        logger.warning(f"Warning Occurred. failed task_house. exception: %s", e)
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
                        continue

                # task終了をDBに登録
                sql = "UPDATE `tasks` SET end_at=NOW(), status=%s WHERE id = %s"
                param = (1, task_id,)
                cursor.execute(sql, param)
                cnx.commit()

            except (Exception,) as e:
                # タスク毎のエラー
                logger.warning("Warning Occurred. failed task: exception: %s", e)

                sql = "UPDATE `tasks` SET end_at=NOW(), status=%s WHERE id = %s"
                param = (-1, task_id,)
                cursor.execute(sql, param)
                cnx.commit()
                break

            logger.debug(f"Completed task. task_id: %s", task_id)

        logger.info("Completed main.")

    except (Exception,) as e:
        logger.error("Error Occurred. exception: %s", e)
        exit(1)
    finally:
        if cnx is not None and cnx.is_connected():
            cnx.close()
            logger.debug("Closed Mysql!")


def update_task_houses(cnx, cursor, p_task_house_id, p_status, p_progress):
    m_sql = "UPDATE `task_houses` SET status = %s, progress = %s, updated_at = NOW() WHERE id = %s"
    m_param = (p_status, p_progress, p_task_house_id,)
    cursor.execute(m_sql, m_param)
    cnx.commit()


def api_main(args):
    age = args.age
    male = args.sex
    edu = args.education
    solo = args.solo
    csv_path = args.data_path
    debug = False

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
#    return model.pred(age=args.age, male=args.male, edu=args.edu, df=df)


class Args:
    def __init__(self, age, male, edu, solo, csv):
        if male != 1:
            male = 0  # 女性は0

        if edu == '小卒':
            edu = 6
        elif edu == '中卒':
            edu = 9
        elif edu == '高卒':
            edu = 12
        elif edu == '大卒':
            edu = 16
        elif edu == '院卒（修士）':
            edu = 17
        elif edu == '院卒（博士）':
            edu = 22
        else:
            edu = 10
        self.age = age
        self.male = male
        self.edu = edu
        self.solo = solo
        self.csv = csv


if __name__ == '__main__':
    main()
