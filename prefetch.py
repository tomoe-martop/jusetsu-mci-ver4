# -*- coding: utf-8 -*-
"""EGPF 1分値の事前取得ジョブ（EG_DASHBOARD-102）。

月次の MCI 予測（Ver3・Ver4）が使う EGPF estimated_data（1分値）を、毎日 05:00（JST）に前日分だけ
取得して GCS の共有キャッシュ（api/egpf_cache.py）に保存する。直近 EGPF_PREFETCH_CATCHUP_DAYS 日のうち
キャッシュが無い日（前日までのジョブ失敗による取りこぼし）も補填する。予測処理は行わない。

設計書: docs/EG_DASHBOARD-102 【高負荷対応】MCI用の事前データ取得/設計書_EGPF1分値の事前取得・キャッシュ設計.md
（3.3・3.7）

- Ver4 と同じイメージの別 Cloud Run Job（`--command python --args prefetch.py`）として動かす
- EGPF_CACHE_BUCKET が無ければ何もせず終了コード 1。EGPF_CACHE_ENABLED（予測時にキャッシュを使うか）には依存しない
- 取得対象日: JST の前日から遡って EGPF_PREFETCH_CATCHUP_DAYS 日（前日を含む）のうち、日付が 1〜28 日のもの
- 対象ハウス: 月次タスク（batch-resources System_2001.php:76-139）と同じ条件を SQL で再現（Ver3・Ver4 両方）
- 負荷制御: ハウス・日とも直列。リクエスト間に EGPF_PREFETCH_INTERVAL_SEC 秒待ち、
  1回の実行のリクエスト数が EGPF_PREFETCH_MAX_REQUESTS に達するか、開始から EGPF_PREFETCH_MAX_MINUTES 分を
  過ぎたら残りは翌日の実行に回す（終了コード 0）
- 失敗はログのみ（Slack 通知はしない）。終了コード: 正常・部分失敗・上限での繰り越し 0、
  連続失敗による打ち切り・キャッシュの遮断（EgpfCache.disabled）・バケット未設定・DB 接続失敗・
  1 件も保存できず GCS の失敗（一覧取得・保存）があった場合 1

main.py を import しない（pred_mci の読み込みと predictor.log へのログ設定が走るため）。
DB 接続・EGPF リクエストの組み立ては main.py と同じ内容を本ファイルに持つ（main.py を変更しないため）。
"""
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

# main.py と同じく api/ を import パスに入れる（egpf_common / egpf_cache は api/ 配下の単一ファイル）
base_dir = os.path.dirname(os.path.abspath(__file__))
api_dir = os.path.join(base_dir, 'api')
if api_dir not in sys.path:
    sys.path.insert(0, api_dir)

from egpf_common import egpf_get, EgpfClientError, EgpfRetryExhausted, ConsecutiveFailureGuard  # noqa: E402
import egpf_cache  # noqa: E402

# ---------------------------------------------------------------------------
# 環境変数（設計書 3.7）
# ---------------------------------------------------------------------------
ENV_INTERVAL_SEC = "EGPF_PREFETCH_INTERVAL_SEC"
ENV_CATCHUP_DAYS = "EGPF_PREFETCH_CATCHUP_DAYS"
ENV_MAX_REQUESTS = "EGPF_PREFETCH_MAX_REQUESTS"
# 開始からの経過時間の上限（分）。超えたら残りを翌日の実行に回して正常終了する（設計書 5.2 の 7:00 越え対策）
ENV_MAX_MINUTES = "EGPF_PREFETCH_MAX_MINUTES"
# 試験用: 「今日」を YYYY-MM-DD で上書きする（STG 結合試験で数日分を続けて実行するため。設計書には無い）
ENV_BASE_DATE = "EGPF_PREFETCH_BASE_DATE"

DEFAULT_INTERVAL_SEC = 1.0
DEFAULT_CATCHUP_DAYS = 7
DEFAULT_MAX_REQUESTS = 4000
DEFAULT_MAX_MINUTES = 100.0
MIN_MAX_MINUTES = 1.0

# 打ち切り理由（Prefetch summary の stop_reason）
STOP_NONE = "none"
STOP_MAX_REQUESTS = "max_requests"
STOP_MAX_MINUTES = "max_minutes"
STOP_GUARD = "guard"
STOP_CACHE_DISABLED = "cache_disabled"

# 月次タスクの期間が前月 1〜28 日で固定のため、29〜31 日分は取得しない（設計書 3.3）
LAST_TARGET_DAY_OF_MONTH = 28

# 対象ハウスの抽出条件（System_2001.php: 2001=MCI 月次、2003 も同じ扱い／algorithm 3=Ver3, 4=Ver4）
MCI_MONITOR_IDS = ("2001", "2003")
MCI_ALGORITHMS = ("3", "4")

# MySQL 接続の再試行（main.py と同じ環境変数・既定値）
ENV_MYSQL_CONNECT_ATTEMPTS = "MCI_MYSQL_CONNECT_ATTEMPTS"
ENV_MYSQL_CONNECT_RETRY_WAIT_SEC = "MCI_MYSQL_CONNECT_RETRY_WAIT_SEC"
DEFAULT_MYSQL_CONNECT_ATTEMPTS = 3
DEFAULT_MYSQL_CONNECT_RETRY_WAIT_SEC = 2.0

DEFAULT_API_URL = "https://api.energy-gateway.jp/0.2/estimated_data"

logger = logging.getLogger("prefetch")


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
def configure_logging():
    """標準出力（ERROR 未満）・標準エラー出力（ERROR 以上）に出す。LOG_LEVEL（既定 INFO）は main.py と同じ。"""
    log_level_name = os.environ.get('LOG_LEVEL', 'INFO').upper()
    log_level = logging.getLevelName(log_level_name)
    invalid_level = not isinstance(log_level, int)
    if invalid_level:
        log_level = logging.INFO
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    root = logging.getLogger()
    for handler in list(root.handlers):
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
    if invalid_level:
        logger.warning("LOG_LEVEL=%r is not a valid level. using INFO", log_level_name)


def _env_number(name, default, cast, minimum):
    """環境変数を数値で読む。未設定・不正値・下限未満は既定値（main.py の _env_number と同じ）。"""
    raw = (os.environ.get(name) or '').strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def resolve_today(now=None):
    """JST の「今日」。EGPF_PREFETCH_BASE_DATE（YYYY-MM-DD）があればその日付。不正値は警告して実時刻。"""
    raw = (os.environ.get(ENV_BASE_DATE) or '').strip()
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            logger.warning("%s=%r is not YYYY-MM-DD. using current date", ENV_BASE_DATE, raw)
    if now is None:
        now = datetime.now(egpf_cache.JST)
    return now.astimezone(egpf_cache.JST).date()


def target_days(today, catchup_days):
    """前日から遡って catchup_days 日（前日を含む）のうち、日付が 1〜28 日のものを古い順に返す。"""
    days = [today - timedelta(days=offset) for offset in range(1, catchup_days + 1)]
    return sorted(d for d in days if 1 <= d.day <= LAST_TARGET_DAY_OF_MONTH)


# ---------------------------------------------------------------------------
# DB（main.py と同じ環境変数・接続の再試行）
# ---------------------------------------------------------------------------
def build_connection_params():
    mysql_host = os.environ.get('MCI_MYSQL_HOST')
    params = {
        'user': os.environ.get('MCI_MYSQL_USER'),
        'password': os.environ.get('MCI_MYSQL_PASSWORD'),
        'database': os.environ.get('MCI_MYSQL_DATABASE'),
        'time_zone': os.environ.get('MCI_MYSQL_TIMEZONE', "Asia/Tokyo"),
    }
    if mysql_host and mysql_host.startswith('/cloudsql/'):
        params['unix_socket'] = mysql_host
    else:
        params['host'] = mysql_host
    return params


def _mysql_connect(**params):
    import mysql.connector
    return mysql.connector.connect(**params)


def connect_mysql(connection_params):
    """MySQL へ接続する。失敗したら固定間隔で再試行し、すべて失敗したら最後の例外を投げる（main.py と同じ）。"""
    attempts = _env_number(ENV_MYSQL_CONNECT_ATTEMPTS, DEFAULT_MYSQL_CONNECT_ATTEMPTS, int, 1)
    wait_sec = _env_number(ENV_MYSQL_CONNECT_RETRY_WAIT_SEC, DEFAULT_MYSQL_CONNECT_RETRY_WAIT_SEC, float, 0)
    for attempt in range(1, attempts + 1):
        try:
            return _mysql_connect(**connection_params)
        except Exception as e:
            if attempt >= attempts:
                logger.warning("MySQL connect attempt=%d/%d failed, giving up: %s: %s",
                               attempt, attempts, type(e).__name__, e)
                raise
            logger.warning("MySQL connect attempt=%d/%d failed: %s: %s retry_in=%ss",
                           attempt, attempts, type(e).__name__, e, wait_sec)
            time.sleep(wait_sec)


def _split_csv(value):
    """カンマ区切りを展開する（前後空白除去・空要素除外）。"""
    if value is None:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _query(cursor, sql, params=None):
    cursor.execute(sql, params)
    return cursor.fetchall()


def fetch_target_houses(cursor):
    """月次タスクと同じ条件の対象ハウスを [(spid, houseid)] で返す（System_2001.php:76-139 の再現）。

    ハウスごとにクエリを発行せず、テーブル単位でまとめて取得してアプリ側で突き合わせる。
    1. 事業者: companies.api_id 非 NULL・menu_mcialert = 1、monitors に spid = api_id・monitorid 2001/2003・
       alertmask = 1 の行がある。その事業者で ON の monitorid を持つ
    2. active_sensors（spid ごとに id 最小の 1 行）の houseids をカンマで展開
    3. housesmask_settings に同じ spid・houseid・（事業者で ON の monitorid のいずれか）・alertmask = 1 の行がある
    4. equipment_cognitive_functions（spid・houseid ごとに id 最小の 1 行）の algorithm に 3 または 4 を含む
    順序は事業者（companies.id）順・houseids の並び順。重複は除く。
    """
    placeholders = ", ".join(["%s"] * len(MCI_MONITOR_IDS))

    companies = _query(
        cursor,
        "SELECT id, api_id FROM `companies` WHERE api_id IS NOT NULL AND menu_mcialert = 1 ORDER BY id")

    monitors_on = {}  # spid -> {monitorid}
    for spid, monitorid in _query(
            cursor,
            "SELECT spid, monitorid FROM `monitors` WHERE monitorid IN (%s) AND alertmask = 1" % placeholders,
            MCI_MONITOR_IDS):
        monitors_on.setdefault(str(spid), set()).add(str(monitorid))

    active_houses = {}  # spid -> [houseid]（id 最小の行）
    for _id, spid, houseids in _query(cursor, "SELECT id, spid, houseids FROM `active_sensors` ORDER BY id"):
        active_houses.setdefault(str(spid), _split_csv(houseids))

    mask_on = {}  # (spid, houseid) -> {monitorid}
    for spid, houseid, monitorid in _query(
            cursor,
            "SELECT spid, houseid, monitorid FROM `housesmask_settings` "
            "WHERE monitorid IN (%s) AND alertmask = 1" % placeholders,
            MCI_MONITOR_IDS):
        mask_on.setdefault((str(spid), str(houseid)), set()).add(str(monitorid))

    algorithms = {}  # (spid, houseid) -> [algorithm]（id 最小の行）
    for _id, spid, houseid, algorithm in _query(
            cursor, "SELECT id, spid, houseid, algorithm FROM `equipment_cognitive_functions` ORDER BY id"):
        algorithms.setdefault((str(spid), str(houseid)), _split_csv(algorithm))

    houses = []
    seen = set()
    for _company_id, api_id in companies:
        spid = str(api_id)
        monitor_ids = monitors_on.get(spid)
        if not monitor_ids:
            continue
        if spid not in active_houses:
            logger.warning("active_sensors has no row for spid=%s (skipped)", spid)
            continue
        for houseid in active_houses[spid]:
            key = (spid, houseid)
            if key in seen:
                continue
            if not (mask_on.get(key, set()) & monitor_ids):
                continue
            if not any(a in MCI_ALGORITHMS for a in algorithms.get(key, [])):
                continue
            seen.add(key)
            houses.append(key)
    return houses


# ---------------------------------------------------------------------------
# EGPF リクエスト（main.py の日次ループと同じ組み立て）
# ---------------------------------------------------------------------------
def _fetch_id_token(audience):
    import google.auth.transport.requests
    import google.oauth2.id_token
    return google.oauth2.id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)


def build_request(spid, houseid, sts, ets, api_url, mock_api_url):
    """(url, headers, params) を返す。spid=9991 かつ MOCK_API_URL があればモックサーバと ID token を使う。"""
    headers = {'Authorization': f"imSP {spid}:{os.environ.get('API_SHARED_PASSWORD')}"}
    params = {'service_provider': spid, 'house': houseid, 'sts': int(sts), 'ets': int(ets), 'time_units': 20}
    if str(spid) == '9991' and mock_api_url:
        url = mock_api_url
        # mock-api-stg が IAM(run.invoker) 保護されている場合の ID token を X-Serverless-Authorization で付与
        try:
            parsed = urlparse(mock_api_url)
            id_tok = _fetch_id_token(f"{parsed.scheme}://{parsed.netloc}")
            headers['X-Serverless-Authorization'] = f'Bearer {id_tok}'
        except Exception as e:
            logger.warning(f"mock-api-stg向けID token取得失敗(認証なしで継続): {e}")
    else:
        url = api_url
    return url, headers, params


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------
def run(now=None, clock=None):
    """事前取得を 1 回実行し、終了コードを返す。

    clock は経過時間の計測に使う関数（既定 time.monotonic。テストで差し替える）。
    """
    if clock is None:
        clock = time.monotonic
    started = clock()
    logger.info("Start prefetch.")

    if egpf_cache.get_bucket_name() is None:
        logger.error("%s is not set. nothing to do", egpf_cache.ENV_CACHE_BUCKET)
        return 1
    cache = egpf_cache.create_cache(writable=True, logger=logger)

    interval_sec = _env_number(ENV_INTERVAL_SEC, DEFAULT_INTERVAL_SEC, float, 0)
    catchup_days = _env_number(ENV_CATCHUP_DAYS, DEFAULT_CATCHUP_DAYS, int, 1)
    max_requests = _env_number(ENV_MAX_REQUESTS, DEFAULT_MAX_REQUESTS, int, 1)
    max_minutes = _env_number(ENV_MAX_MINUTES, DEFAULT_MAX_MINUTES, float, MIN_MAX_MINUTES)
    api_url = os.environ.get('ENERGY_GATEWAY_API_URL', DEFAULT_API_URL)
    mock_api_url = os.environ.get('MOCK_API_URL')

    today = resolve_today(now)
    days = target_days(today, catchup_days)
    # 終わっていない日（EGPF_PREFETCH_BASE_DATE に明日以降を指定した場合など）は取りに行かない。
    # egpf_cache.save も保存しないため、取得しても EGPF への無駄なリクエストになる
    now_epoch = now.timestamp() if now is not None else time.time()
    unfinished = [d for d in days if egpf_cache.day_bounds(d)[1] > now_epoch]
    if unfinished:
        logger.warning("skip unfinished days (not fetched): %s",
                       ",".join(d.strftime("%Y-%m-%d") for d in unfinished))
        days = [d for d in days if d not in unfinished]
    logger.info("prefetch settings: bucket=%s today=%s catchup_days=%s interval_sec=%s max_requests=%s "
                "max_minutes=%s days=%s",
                cache.bucket_name, today, catchup_days, interval_sec, max_requests, max_minutes,
                ",".join(d.strftime("%Y-%m-%d") for d in days) or "-")

    # 対象ハウス（DB は抽出後すぐ閉じる。取得は長時間かかるため接続を持ち続けない）
    cnx = None
    try:
        cnx = connect_mysql(build_connection_params())
        houses = fetch_target_houses(cnx.cursor())
    except Exception as e:
        logger.error("Failed to load target houses from DB: %s: %s", type(e).__name__, e)
        return 1
    finally:
        if cnx is not None:
            try:
                cnx.close()
            except Exception:
                pass

    # failed = 保存できなかった件数の合計（EGPF 取得失敗・JSON でない・検証 NG・GCS への保存失敗）
    # invalid = うち検証 NG（空レスポンス等で保存しなかった）、save_failed = うち GCS への保存失敗
    stats = {
        'houses': len(houses), 'skipped_cached': 0, 'requests': 0, 'saved': 0, 'failed': 0,
        'invalid': 0, 'save_failed': 0,
        'list_failed_houses': 0, 'carried_over': 0, 'unchecked_houses': 0,
    }
    guard = ConsecutiveFailureGuard()
    stop_reason = STOP_NONE

    def check_stop(count_requests):
        """打ち切り理由を返す（無ければ None）。count_requests=False ならリクエスト数の上限は見ない。"""
        if getattr(cache, "disabled", False):
            return STOP_CACHE_DISABLED
        if count_requests and stats['requests'] >= max_requests:
            return STOP_MAX_REQUESTS
        if clock() - started > max_minutes * 60:
            return STOP_MAX_MINUTES
        return None

    for house_index, (spid, houseid) in enumerate(houses):
        if not days:
            break
        # ハウスの処理前: キャッシュ遮断・時間切れなら一覧取得もしない（このハウスから未確認）
        reason = check_stop(count_requests=False)
        if reason is not None:
            stop_reason = reason
            stats['unchecked_houses'] = len(houses) - house_index
            break
        cached = cache.list_cached_days(spid, houseid)
        if cached is None:
            logger.warning("Skip house (cannot list cache): spid=%s house=%s", spid, houseid)
            stats['list_failed_houses'] += 1
            continue
        pending = [d for d in days if d not in cached]
        stats['skipped_cached'] += len(days) - len(pending)

        for day_index, day in enumerate(pending):
            reason = check_stop(count_requests=True)
            if reason is not None:
                stop_reason = reason
                stats['carried_over'] = len(pending) - day_index
                stats['unchecked_houses'] = len(houses) - house_index - 1
                break
            if stats['requests'] > 0 and interval_sec > 0:
                time.sleep(interval_sec)
            stats['requests'] += 1
            if not fetch_and_save(cache, guard, spid, houseid, day, api_url, mock_api_url, stats):
                stop_reason = STOP_GUARD
                stats['unchecked_houses'] = len(houses) - house_index - 1
                break
        if stop_reason != STOP_NONE:
            break

    # 最後の保存でキャッシュが遮断された場合も異常として扱う（以後の実行も GCS に書けない可能性が高い）
    if stop_reason == STOP_NONE and getattr(cache, "disabled", False):
        stop_reason = STOP_CACHE_DISABLED

    elapsed = clock() - started
    if stop_reason == STOP_MAX_REQUESTS:
        logger.warning("Reached %s=%s. carried over to the next run: days=%s (spid/house in progress) "
                       "+ unchecked houses=%s", ENV_MAX_REQUESTS, max_requests, stats['carried_over'],
                       stats['unchecked_houses'])
    elif stop_reason == STOP_MAX_MINUTES:
        logger.warning("Reached %s=%s (elapsed=%.1fs). carried over to the next run: days=%s "
                       "(spid/house in progress) + unchecked houses=%s", ENV_MAX_MINUTES, max_minutes, elapsed,
                       stats['carried_over'], stats['unchecked_houses'])
    aborted = stop_reason in (STOP_GUARD, STOP_CACHE_DISABLED)
    logger.info("Prefetch summary: houses=%s days=%s skipped_cached=%s requests=%s saved=%s failed=%s "
                "invalid=%s save_failed=%s list_failed_houses=%s carried_over=%s unchecked_houses=%s "
                "stop_reason=%s aborted=%s elapsed=%.1fs",
                stats['houses'], ",".join(d.strftime("%Y-%m-%d") for d in days) or "-",
                stats['skipped_cached'], stats['requests'], stats['saved'], stats['failed'],
                stats['invalid'], stats['save_failed'], stats['list_failed_houses'], stats['carried_over'],
                stats['unchecked_houses'], stop_reason, aborted, elapsed)
    if stop_reason == STOP_GUARD:
        logger.error("Aborted prefetch: consecutive EGPF retry exhaustion. unchecked houses=%s",
                     stats['unchecked_houses'])
        return 1
    if stop_reason == STOP_CACHE_DISABLED:
        logger.error("Aborted prefetch: EGPF cache is disabled after consecutive GCS failures. "
                     "carried over days=%s + unchecked houses=%s", stats['carried_over'], stats['unchecked_houses'])
        return 1
    # 1 件も保存できず、GCS の障害（一覧取得の失敗・保存失敗）があった: バケット名・IAM の誤り等を疑う
    if stats['saved'] == 0 and (stats['list_failed_houses'] > 0 or stats['save_failed'] > 0):
        logger.error("Prefetch saved nothing with GCS failures: list_failed_houses=%s save_failed=%s "
                     "(check bucket name and IAM)", stats['list_failed_houses'], stats['save_failed'])
        return 1
    logger.info("Completed prefetch.")
    return 0


def fetch_and_save(cache, guard, spid, houseid, day, api_url, mock_api_url, stats):
    """1 日分を EGPF から取得して保存する。連続失敗で打ち切るべきなら False を返す。"""
    sts, ets = egpf_cache.day_bounds(day)
    url, headers, params = build_request(spid, houseid, sts, ets, api_url, mock_api_url)
    context = {'spid': spid, 'house': houseid, 'sts': sts, 'ets': ets}
    try:
        res = egpf_get(url, headers, params, context=context, logger=logger)
    except EgpfClientError as e:
        # 4xx は EGPF 障害ではないので連続失敗に数えない（main.py と同じ）
        guard.record_success()
        stats['failed'] += 1
        logger.warning("Prefetch failed (client error): spid=%s house=%s date=%s %s", spid, houseid, day, e)
        return True
    except EgpfRetryExhausted as e:
        stats['failed'] += 1
        logger.warning("Prefetch failed (retry exhausted): spid=%s house=%s date=%s %s", spid, houseid, day, e)
        return not guard.record_exhausted()
    except Exception as e:
        stats['failed'] += 1
        logger.warning("Prefetch failed: spid=%s house=%s date=%s %s: %s", spid, houseid, day, type(e).__name__, e)
        return True
    guard.record_success()

    try:
        response_json = res.json()
    except Exception as e:
        stats['failed'] += 1
        logger.warning("Prefetch failed (not JSON): spid=%s house=%s date=%s %s: %s",
                       spid, houseid, day, type(e).__name__, e)
        return True
    # 検証は保存前にここで行う（save の False を GCS 障害と区別するため）
    if not egpf_cache.is_valid_response(response_json, sts, ets):
        stats['failed'] += 1
        stats['invalid'] += 1
        logger.warning("Prefetch not saved (invalid response): spid=%s house=%s date=%s", spid, houseid, day)
        return True
    # 検証済みなので、ここでの False は GCS 障害（書き込み権限なし・遮断中等）
    if cache.save(spid, houseid, day, sts, ets, response_json):
        stats['saved'] += 1
    else:
        stats['failed'] += 1
        stats['save_failed'] += 1
    return True


def main():
    configure_logging()
    try:
        return run()
    except Exception as e:
        logger.exception("Unexpected error in prefetch: %s", e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
