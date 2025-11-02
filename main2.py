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
from typing import Dict, Any

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
except TypeError as e:
    # Python 3.8以前でtuple[...]型ヒントが使えない場合のエラーハンドリング
    if "'type' object is not subscriptable" in str(e):
        import importlib.util
        import re
        
        # 動的にモジュールを読み込む（型ヒントを修正）
        # Docker環境対応: 絶対パスを使用
        api_path = os.path.join(api_dir, 'pred_mci.py')
        
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
        from myexception import InvalidInputError, PredictionError, PredictionTimeOut, UnexpectedError, TIMEOUT, timeout_handler
        
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
        }
        
        # モジュールをコンパイルして実行
        compiled = compile(source_fixed, api_path, 'exec')
        exec(compiled, namespace)
        
        # Predictorクラスを取得
        Predictor = namespace['Predictor']
    else:
        raise
except Exception as e:
    # その他のインポートエラー
    logger.error(f"Predictorクラスのインポートに失敗しました: {e}")
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


def interpret_score(score: int) -> str:
    """
    スコアから判定結果を返す

    :param score: 認知機能スコア（0-100）
    :return: 判定結果
    """
    if score >= 80:
        return "正常"
    elif score >= 60:
        return "軽度認知障害の可能性"
    else:
        return "認知症の可能性"


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


def main():
    """
    メイン処理
    """
    logger.info("=== MCI予測処理開始 ===")

    # 入力パラメータ（api/README.mdのサンプルに基づく）
    age = 70
    male = 0  # 女性
    edu = 12  # 教育年数12年
    solo = 1  # 独居
    # Docker環境対応: 絶対パスまたはbase_dirからの相対パスを使用
    csv_path = os.path.join(base_dir, "api", "csv", "test_data.csv")
    debug = False  # デバッグモードはデフォルトでFalse

    # コマンドライン引数から値を取得（オプション）
    if len(sys.argv) > 1:
        try:
            age = int(sys.argv[1])
            if len(sys.argv) > 2:
                male = int(sys.argv[2])
            if len(sys.argv) > 3:
                edu = int(sys.argv[3])
            if len(sys.argv) > 4:
                solo = int(sys.argv[4])
            if len(sys.argv) > 5:
                csv_path_arg = sys.argv[5]
                # 絶対パスの場合はそのまま、相対パスの場合はbase_dirからの相対パスとして処理
                if os.path.isabs(csv_path_arg):
                    csv_path = csv_path_arg
                else:
                    csv_path = os.path.join(base_dir, csv_path_arg)
            if len(sys.argv) > 6:
                debug = sys.argv[6].lower() in ['true', '1', 'yes']
        except (ValueError, IndexError) as e:
            logger.warning(f"コマンドライン引数の解析に失敗しました。デフォルト値を使用します: {e}")

    # 入力パラメータの表示
    print("\n=== 入力パラメータ ===")
    print(f"年齢: {age}歳")
    print(f"性別: {'男性' if male == 1 else '女性'}")
    print(f"教育年数: {edu}年")
    print(f"独居: {'はい' if solo == 1 else 'いいえ'}")
    print(f"CSVファイル: {csv_path}")
    print(f"デバッグモード: {debug}")
    print()

    try:
        # 予測実行
        result = predict_mci(age, male, edu, solo, csv_path, debug)

        # 結果の表示
        print("=== 予測結果 ===")
        status_code = result.get('status_code')
        score = result.get('score')

        print(f"ステータスコード: {status_code}")
        print(f"ステータス: {get_status_message(status_code)}")

        if status_code == 100:
            # 予測成功
            if debug:
                # デバッグモードの場合、詳細な予測値を表示
                if isinstance(score, dict):
                    print("\n=== 詳細予測値（デバッグモード） ===")
                    print(f"LightGBMモデル予測値: {score.get('lightgbm', 'N/A'):.4f}")
                    print(f"Logistic回帰モデル予測値: {score.get('logistic', 'N/A'):.4f}")
                    print(f"Soft Voting予測値: {score.get('soft_voting', 'N/A'):.4f}")
                    # Soft Votingの値を認知機能スコアとして使用
                    soft_voting = score.get('soft_voting', 0)
                    if isinstance(soft_voting, float):
                        cognitive_score = int(soft_voting * 100)
                        print(f"\n認知機能スコア: {cognitive_score}")
                        print(f"判定: {interpret_score(cognitive_score)}")
            else:
                # 通常モードの場合
                if score is not None:
                    print(f"認知機能スコア: {score}")
                    print(f"判定: {interpret_score(score)}")
        else:
            # エラーの場合
            print(f"\nエラーが発生しました。スコア: {score}")

        logger.info("=== MCI予測処理完了 ===")
        return result

    except Exception as e:
        logger.error(f"予測処理中にエラーが発生しました: {e}")
        print(f"\nエラー: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()

