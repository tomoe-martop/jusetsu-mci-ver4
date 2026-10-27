# -*- coding: utf-8 -*-
"""prefetch.py（EGPF 1分値の事前取得ジョブ）の単体テスト（設計書 3.3・6.1）。

DB はフェイクカーソル、GCS は test_egpf_cache.py の FakeClient、requests.get / time.sleep はモック。
ネットワーク・DB・GCP 資格情報は不要。実行: python -m pytest tests -q
"""
import gzip
import json
import logging
import os
import sys
import time
import types
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

import egpf_cache  # noqa: E402
import prefetch  # noqa: E402
from test_egpf_cache import FakeClient  # noqa: E402

BUCKET = "stg-mci-egpf-cache"
API_URL = "https://api.energy-gateway.jp/0.2/estimated_data"
MOCK_URL = "https://mock-api-stg.example.test/0.2/estimated_data"
TODAY = date(2026, 10, 10)  # 既定の対象日: 10/3〜10/9（前日から 7 日）


# ---------------------------------------------------------------------------
# フェイク DB
# ---------------------------------------------------------------------------
class FakeCursor:
    """prefetch.fetch_target_houses が発行する SELECT にテーブルごとの固定行を返す。"""

    def __init__(self, companies=(), monitors=(), active_sensors=(), masks=(), ecf=()):
        self.tables = {
            "companies": list(companies),             # (id, api_id, menu_mcialert)
            "monitors": list(monitors),               # (spid, monitorid, alertmask)
            "active_sensors": list(active_sensors),   # (id, spid, houseids)
            "housesmask_settings": list(masks),       # (spid, houseid, monitorid, alertmask)
            "equipment_cognitive_functions": list(ecf),  # (id, spid, houseid, algorithm)
        }
        self.executed = []
        self._rows = []

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.executed.append((sql, params))
        if "FROM `companies`" in sql:
            assert "api_id IS NOT NULL" in sql and "menu_mcialert = 1" in sql
            rows = sorted(self.tables["companies"])
            self._rows = [(i, a) for (i, a, menu) in rows if a is not None and menu == 1]
        elif "FROM `monitors`" in sql:
            assert "alertmask = 1" in sql and tuple(params) == ("2001", "2003")
            self._rows = [(s, m) for (s, m, mask) in self.tables["monitors"]
                          if str(m) in params and mask == 1]
        elif "FROM `active_sensors`" in sql:
            self._rows = sorted(self.tables["active_sensors"])
        elif "FROM `housesmask_settings`" in sql:
            assert "alertmask = 1" in sql and tuple(params) == ("2001", "2003")
            self._rows = [(s, h, m) for (s, h, m, mask) in self.tables["housesmask_settings"]
                          if str(m) in params and mask == 1]
        elif "FROM `equipment_cognitive_functions`" in sql:
            self._rows = sorted(self.tables["equipment_cognitive_functions"])
        else:
            raise AssertionError("unexpected SQL: %s" % sql)

    def fetchall(self):
        return list(self._rows)


def _simple_db(houses, spid="0187"):
    """houses の全ハウスが対象条件を満たす DB。"""
    return FakeCursor(
        companies=[(1, spid, 1)],
        monitors=[(spid, "2001", 1)],
        active_sensors=[(1, spid, ",".join(houses))],
        masks=[(spid, h, "2001", 1) for h in houses],
        ecf=[(i + 1, spid, h, "4") for i, h in enumerate(houses)],
    )


# ---------------------------------------------------------------------------
# EGPF レスポンス
# ---------------------------------------------------------------------------
def _ok_json(day, n=3):
    sts, _ = egpf_cache.day_bounds(day)
    return {"data": [{
        "timestamps": [sts + 60 * i for i in range(n)],
        "appliance_types": [{"appliance_type_id": 2, "appliances": [{"powers": [1.0, 0.0, None][:n]}]}],
    }]}


def _resp(status, body=None, json_error=None):
    res = MagicMock(spec=requests.Response)
    res.status_code = status
    res.text = ""
    if json_error is not None:
        res.json.side_effect = json_error
    else:
        res.json.return_value = body
    return res


def _auto_ok(url, headers=None, params=None, timeout=None):
    """params の sts に合わせた正常レスポンス。"""
    day = datetime.fromtimestamp(params["sts"], egpf_cache.JST).date()
    return _resp(200, _ok_json(day))


def _put(client, spid, houseid, day):
    sts, ets = egpf_cache.day_bounds(day)
    env = egpf_cache.build_envelope(spid, houseid, day, sts, ets, _ok_json(day))
    client.put_envelope(egpf_cache.object_key(spid, houseid, day), env)


def _days(*dd):
    return [date(2026, 10, d) for d in dd]


def _requested(get):
    """requests.get の呼び出しを (house, 日付) で返す。"""
    out = []
    for c in get.call_args_list:
        params = c.kwargs["params"]
        out.append((params["house"], datetime.fromtimestamp(params["sts"], egpf_cache.JST).date()))
    return out


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------
@pytest.fixture
def env(monkeypatch):
    for name in ("EGPF_RETRY_MAX_ATTEMPTS", "EGPF_RETRY_WAIT_SEC", "EGPF_CONNECT_TIMEOUT_SEC",
                 "EGPF_READ_TIMEOUT_SEC", "EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES",
                 "MOCK_API_URL", "ENERGY_GATEWAY_API_URL", egpf_cache.ENV_CACHE_ENABLED,
                 prefetch.ENV_INTERVAL_SEC, prefetch.ENV_CATCHUP_DAYS, prefetch.ENV_MAX_REQUESTS,
                 prefetch.ENV_MAX_MINUTES,
                 prefetch.ENV_BASE_DATE, prefetch.ENV_MYSQL_CONNECT_ATTEMPTS,
                 prefetch.ENV_MYSQL_CONNECT_RETRY_WAIT_SEC):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(egpf_cache.ENV_CACHE_BUCKET, BUCKET)
    monkeypatch.setenv(prefetch.ENV_BASE_DATE, TODAY.strftime("%Y-%m-%d"))
    monkeypatch.setenv("API_SHARED_PASSWORD", "pw")
    monkeypatch.setenv("MCI_MYSQL_USER", "u")
    monkeypatch.setenv("MCI_MYSQL_HOST", "db")


@pytest.fixture
def gcs(monkeypatch):
    """create_cache が返す EgpfCache にフェイク GCS を注入する。"""
    client = FakeClient()
    created = []
    real_create_cache = egpf_cache.create_cache

    def create_cache(writable=True, logger=None):
        cache = real_create_cache(writable=writable, logger=logger)
        if cache is not None and getattr(client, "inject", True):
            cache._client = client
        created.append(cache)
        return cache

    monkeypatch.setattr(egpf_cache, "create_cache", create_cache)
    client.created = created
    return client


@pytest.fixture
def db(monkeypatch):
    """prefetch._mysql_connect をフェイクに差し替える。holder.cursor を各テストで設定する。"""
    holder = types.SimpleNamespace(cursor=_simple_db(["H1"]), connect_calls=[], connect_error=None, cnx=None)

    def connect(**params):
        holder.connect_calls.append(params)
        if holder.connect_error is not None:
            raise holder.connect_error
        cnx = MagicMock(name="cnx")
        cnx.cursor.return_value = holder.cursor
        holder.cnx = cnx
        return cnx

    monkeypatch.setattr(prefetch, "_mysql_connect", connect)
    return holder


@pytest.fixture
def caplog_info(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def run_prefetch(get_side_effect=_auto_ok, clock=None):
    """prefetch.run() を実行し (exit_code, get, sleep) を返す。clock を渡すと経過時間の計測に使う。"""
    sleep = MagicMock(name="sleep")
    with patch("requests.get", side_effect=get_side_effect) as get, \
            patch.object(time, "sleep", sleep):
        code = prefetch.run(clock=clock) if clock is not None else prefetch.run()
    return code, get, sleep


def _summary(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Prefetch summary:")]
    assert len(lines) == 1
    return lines[0]


# ---------------------------------------------------------------------------
# 対象日の算出（1〜28 日のみ）
# ---------------------------------------------------------------------------
class TestTargetDays:
    def test_default_seven_days_before_today(self):
        assert prefetch.target_days(TODAY, 7) == _days(3, 4, 5, 6, 7, 8, 9)

    def test_month_start_excludes_29_to_31(self):
        # 11/2 実行: 11/1, 10/31, 10/30, 10/29, 10/28, 10/27, 10/26 → 29〜31 日を除く
        assert prefetch.target_days(date(2026, 11, 2), 7) == [
            date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28), date(2026, 11, 1)]

    def test_run_on_first_day_yesterday_is_month_end(self):
        # 11/1 実行の前日は 10/31（対象外）。前日だけなら対象日なし
        assert prefetch.target_days(date(2026, 11, 1), 1) == []
        assert prefetch.target_days(date(2026, 11, 1), 4) == [date(2026, 10, 28)]

    def test_run_on_29th_includes_28th(self):
        assert prefetch.target_days(date(2026, 10, 29), 1) == [date(2026, 10, 28)]
        assert prefetch.target_days(date(2026, 10, 30), 1) == []

    def test_february_and_leap_year(self):
        assert prefetch.target_days(date(2026, 3, 2), 3) == [date(2026, 2, 27), date(2026, 2, 28), date(2026, 3, 1)]
        # 2028 は閏年: 2/29 は対象外
        assert prefetch.target_days(date(2028, 3, 1), 2) == [date(2028, 2, 28)]

    def test_year_boundary(self):
        assert prefetch.target_days(date(2027, 1, 3), 7) == [
            date(2026, 12, 27), date(2026, 12, 28), date(2027, 1, 1), date(2027, 1, 2)]

    def test_catchup_days_changed(self):
        assert prefetch.target_days(TODAY, 1) == _days(9)
        assert prefetch.target_days(TODAY, 3) == _days(7, 8, 9)
        assert prefetch.target_days(date(2026, 10, 20), 30) == (
            [date(2026, 9, d) for d in range(20, 29)] + _days(*range(1, 20)))

    def test_catchup_env_invalid_or_below_minimum_uses_default(self, env, gcs, db, monkeypatch, caplog_info):
        for raw in ("abc", "0", "-1", "1.5", ""):
            monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, raw)
            assert prefetch._env_number(prefetch.ENV_CATCHUP_DAYS, 7, int, 1) == 7
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "2")
        code, get, sleep = run_prefetch()
        assert code == 0
        assert _requested(get) == [("H1", date(2026, 10, 8)), ("H1", date(2026, 10, 9))]


class TestResolveToday:
    def test_now_in_jst(self, monkeypatch):
        monkeypatch.delenv(prefetch.ENV_BASE_DATE, raising=False)
        # UTC 2026-10-09 20:00 = JST 2026-10-10 05:00
        now = datetime(2026, 10, 9, 20, 0, tzinfo=egpf_cache.timezone.utc)
        assert prefetch.resolve_today(now) == TODAY

    def test_base_date_override(self, monkeypatch):
        monkeypatch.setenv(prefetch.ENV_BASE_DATE, " 2026-10-05 ")
        assert prefetch.resolve_today(datetime(2026, 1, 1, tzinfo=egpf_cache.JST)) == date(2026, 10, 5)

    def test_invalid_base_date_warns_and_uses_now(self, monkeypatch, caplog_info):
        monkeypatch.setenv(prefetch.ENV_BASE_DATE, "2026/10/05")
        assert prefetch.resolve_today(datetime(2026, 10, 10, 5, 0, tzinfo=egpf_cache.JST)) == TODAY
        assert "EGPF_PREFETCH_BASE_DATE='2026/10/05' is not YYYY-MM-DD" in caplog_info.text


# ---------------------------------------------------------------------------
# 対象ハウス抽出（System_2001.php:76-139）
# ---------------------------------------------------------------------------
class TestTargetHouses:
    def test_four_conditions(self):
        cursor = FakeCursor(
            companies=[
                (1, "0187", 1),   # 対象事業者（2001 ON）
                (2, "0200", 1),   # 2003 だけ ON
                (3, "0300", 0),   # menu_mcialert = 0
                (4, None, 1),     # api_id NULL
                (5, "0400", 1),   # monitors が OFF のみ
            ],
            monitors=[
                ("0187", "2001", 1), ("0187", "2003", 0),
                ("0200", "2003", 1), ("0200", "2001", 0),
                ("0300", "2001", 1),
                ("0400", "2001", 0), ("0400", "1001", 1),
            ],
            active_sensors=[
                (1, "0187", " A1, A2 ,,A3,A4, A5,A6 "),
                (2, "0200", "B1,B2"),
                (3, "0300", "C1"),
                (4, "0400", "D1"),
                (9, "0187", "X9"),  # 同じ spid の 2 行目（id 最小の行だけ使う）
            ],
            masks=[
                ("0187", "A1", "2001", 1),
                ("0187", "A2", "2001", 1),
                ("0187", "A3", "2003", 1),  # 0187 では 2003 が OFF → 対象外
                ("0187", "A4", "2001", 0),  # alertmask = 0 → 対象外
                ("0187", "A5", "2001", 1),
                ("0187", "A6", "2001", 1),
                ("0187", "X9", "2001", 1),
                ("0200", "B1", "2003", 1),
                ("0200", "B2", "2001", 1),  # 0200 では 2001 が OFF → 対象外
                ("0300", "C1", "2001", 1),
                ("0400", "D1", "2001", 1),
            ],
            ecf=[
                (1, "0187", "A1", "4"),
                (2, "0187", "A2", "1,3"),
                (3, "0187", "A3", "4"),
                (4, "0187", "A4", "4"),
                (5, "0187", "A5", "1,2"),   # 3/4 を含まない → 対象外
                # A6 は ecf の行が無い → 対象外
                (7, "0187", "X9", "4"),
                (8, "0200", "B1", " 3 , 4 "),
                (9, "0200", "B2", "4"),
                (10, "0300", "C1", "4"),
                (11, "0400", "D1", "4"),
                (12, "0187", "A5", "4"),     # 2 行目（id 最小の行だけ使う）
            ],
        )
        assert prefetch.fetch_target_houses(cursor) == [("0187", "A1"), ("0187", "A2"), ("0200", "B1")]
        # テーブル単位でまとめて取得（ハウスごとのクエリは無い）
        assert len(cursor.executed) == 5

    def test_monitorid_and_spid_types_are_normalized(self):
        # monitorid が数値型・spid が数値型で返っても突き合わせできる
        cursor = FakeCursor(
            companies=[(1, 187, 1)],
            monitors=[(187, 2001, 1)],
            active_sensors=[(1, 187, "H1")],
            masks=[(187, "H1", 2001, 1)],
            ecf=[(1, 187, "H1", "4")],
        )
        # FakeCursor のフィルタは str 比較なので monitorid は文字列化して判定される
        assert prefetch.fetch_target_houses(cursor) == [("187", "H1")]

    def test_duplicate_company_and_house_are_deduplicated(self):
        cursor = FakeCursor(
            companies=[(1, "0187", 1), (2, "0187", 1)],
            monitors=[("0187", "2001", 1)],
            active_sensors=[(1, "0187", "H1,H2,H1")],
            masks=[("0187", "H1", "2001", 1), ("0187", "H2", "2001", 1)],
            ecf=[(1, "0187", "H1", "3"), (2, "0187", "H2", "4")],
        )
        assert prefetch.fetch_target_houses(cursor) == [("0187", "H1"), ("0187", "H2")]

    def test_company_without_active_sensors_or_houseids(self):
        cursor = FakeCursor(
            companies=[(1, "0187", 1), (2, "0200", 1)],
            monitors=[("0187", "2001", 1), ("0200", "2001", 1)],
            active_sensors=[(1, "0200", None)],
            masks=[("0187", "H1", "2001", 1)],
            ecf=[(1, "0187", "H1", "4")],
        )
        assert prefetch.fetch_target_houses(cursor) == []


# ---------------------------------------------------------------------------
# 取得・補填・スキップ
# ---------------------------------------------------------------------------
class TestFetch:
    def test_fetches_only_missing_days_and_saves(self, env, gcs, db, caplog_info):
        db.cursor = _simple_db(["H1", "H2"])
        for d in (3, 5, 9):
            _put(gcs, "0187", "H1", date(2026, 10, d))
        for d in (3, 4, 5, 6, 7, 8, 9):
            _put(gcs, "0187", "H2", date(2026, 10, d))
        uploads_before = gcs.upload_calls

        code, get, sleep = run_prefetch()

        assert code == 0
        # 取りこぼし補填: H1 の無い日だけ古い順に取得。H2 は全日保存済みなので取り直さない
        assert _requested(get) == [("H1", date(2026, 10, d)) for d in (4, 6, 7, 8)]
        assert gcs.upload_calls - uploads_before == 4
        assert prefetch_saved_days(gcs, "0187", "H1") == _days(3, 4, 5, 6, 7, 8, 9)
        # 保存した封筒が予測時の load で読める
        sts, ets = egpf_cache.day_bounds(date(2026, 10, 4))
        cache = egpf_cache.EgpfCache(BUCKET, client=gcs)
        assert cache.load("0187", "H1", date(2026, 10, 4), sts, ets) == _ok_json(date(2026, 10, 4))
        summary = _summary(caplog_info)
        assert "houses=2 " in summary and "skipped_cached=10 " in summary
        assert "requests=4 saved=4 failed=0 " in summary and "carried_over=0 " in summary
        assert "elapsed=" in summary

    def test_request_shape_same_as_main(self, env, gcs, db, monkeypatch):
        env_days = prefetch.target_days(TODAY, 1)
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "1")
        code, get, sleep = run_prefetch()
        assert code == 0
        sts, ets = egpf_cache.day_bounds(env_days[0])
        call = get.call_args
        assert call.args[0] == API_URL
        assert call.kwargs["headers"] == {"Authorization": "imSP 0187:pw"}
        assert call.kwargs["params"] == {"service_provider": "0187", "house": "H1", "sts": sts, "ets": ets,
                                         "time_units": 20}
        assert call.kwargs["timeout"] == (10.0, 30.0)

    def test_mock_spid_uses_mock_url_and_id_token(self, env, gcs, db, monkeypatch):
        monkeypatch.setenv("MOCK_API_URL", MOCK_URL)
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "1")
        db.cursor = FakeCursor(
            companies=[(1, "9991", 1), (2, "0187", 1)],
            monitors=[("9991", "2001", 1), ("0187", "2001", 1)],
            active_sensors=[(1, "9991", "M1"), (2, "0187", "H1")],
            masks=[("9991", "M1", "2001", 1), ("0187", "H1", "2001", 1)],
            ecf=[(1, "9991", "M1", "4"), (2, "0187", "H1", "3")],
        )
        audiences = []
        monkeypatch.setattr(prefetch, "_fetch_id_token", lambda aud: audiences.append(aud) or "tok")

        code, get, sleep = run_prefetch()

        assert code == 0
        mock_call, real_call = get.call_args_list
        assert mock_call.args[0] == MOCK_URL
        assert mock_call.kwargs["headers"] == {"Authorization": "imSP 9991:pw",
                                               "X-Serverless-Authorization": "Bearer tok"}
        assert audiences == ["https://mock-api-stg.example.test"]
        assert real_call.args[0] == API_URL
        assert real_call.kwargs["headers"] == {"Authorization": "imSP 0187:pw"}

    def test_mock_id_token_failure_continues(self, env, gcs, db, monkeypatch, caplog_info):
        monkeypatch.setenv("MOCK_API_URL", MOCK_URL)
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "1")
        db.cursor = _simple_db(["M1"], spid="9991")

        def boom(aud):
            raise RuntimeError("no creds")

        monkeypatch.setattr(prefetch, "_fetch_id_token", boom)
        code, get, sleep = run_prefetch()
        assert code == 0
        assert get.call_args.kwargs["headers"] == {"Authorization": "imSP 9991:pw"}
        assert "ID token取得失敗(認証なしで継続): no creds" in caplog_info.text

    def test_cache_enabled_flag_is_not_required(self, env, gcs, db, monkeypatch):
        # EGPF_CACHE_ENABLED が OFF（未設定・false）でも事前取得は動き、保存する
        for raw in (None, "false"):
            if raw is None:
                monkeypatch.delenv(egpf_cache.ENV_CACHE_ENABLED, raising=False)
            else:
                monkeypatch.setenv(egpf_cache.ENV_CACHE_ENABLED, raw)
            gcs.objects.clear()
            code, get, sleep = run_prefetch()
            assert code == 0
            assert get.call_count == 7
            assert len(gcs.objects) == 7
        assert all(c.writable for c in gcs.created)

    def test_db_connection_params_same_env_as_main(self, env, gcs, db, monkeypatch):
        monkeypatch.setenv("MCI_MYSQL_HOST", "/cloudsql/p:r:i")
        monkeypatch.setenv("MCI_MYSQL_PASSWORD", "dbpw")
        monkeypatch.setenv("MCI_MYSQL_DATABASE", "test_mci")
        run_prefetch()
        assert db.connect_calls == [{"user": "u", "password": "dbpw", "database": "test_mci",
                                     "time_zone": "Asia/Tokyo", "unix_socket": "/cloudsql/p:r:i"}]
        db.cnx.close.assert_called_once()

    def test_no_target_houses(self, env, gcs, db, caplog_info):
        db.cursor = FakeCursor()
        code, get, sleep = run_prefetch()
        assert code == 0
        get.assert_not_called()
        assert "houses=0 " in _summary(caplog_info)


def prefetch_saved_days(client, spid, houseid):
    prefix = "estimated_data/v1/%s/%s/" % (spid, houseid)
    return sorted(datetime.strptime(k[len(prefix):-len(".json.gz")], "%Y%m%d").date()
                  for k in client.objects if k.startswith(prefix))


# ---------------------------------------------------------------------------
# 負荷制御（待ち・最大リクエスト数）
# ---------------------------------------------------------------------------
class TestThrottle:
    def test_sleep_between_requests_only(self, env, gcs, db):
        db.cursor = _simple_db(["H1", "H2"])
        for d in (3, 4, 5, 6, 7, 8):
            _put(gcs, "0187", "H1", date(2026, 10, d))   # H1 は 10/9 だけ取得
        for d in (3, 4, 5, 6, 7):
            _put(gcs, "0187", "H2", date(2026, 10, d))   # H2 は 10/8, 10/9 を取得
        code, get, sleep = run_prefetch()
        assert code == 0
        assert get.call_count == 3
        # リクエスト間だけ待つ（最初のリクエストの前・キャッシュ済みの日では待たない）
        assert sleep.call_args_list == [((1.0,),), ((1.0,),)]

    def test_interval_env(self, env, gcs, db, monkeypatch):
        monkeypatch.setenv(prefetch.ENV_INTERVAL_SEC, "0.5")
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "3")
        code, get, sleep = run_prefetch()
        assert sleep.call_args_list == [((0.5,),), ((0.5,),)]

    def test_interval_zero_does_not_sleep_and_invalid_uses_default(self, env, gcs, db, monkeypatch):
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "3")
        monkeypatch.setenv(prefetch.ENV_INTERVAL_SEC, "0")
        code, get, sleep = run_prefetch()
        assert get.call_count == 3
        sleep.assert_not_called()
        gcs.objects.clear()
        monkeypatch.setenv(prefetch.ENV_INTERVAL_SEC, "-1")
        code, get, sleep = run_prefetch()
        assert sleep.call_args_list == [((1.0,),), ((1.0,),)]

    def test_all_cached_no_requests_no_sleep(self, env, gcs, db, caplog_info):
        for d in range(3, 10):
            _put(gcs, "0187", "H1", date(2026, 10, d))
        code, get, sleep = run_prefetch()
        assert code == 0
        get.assert_not_called()
        sleep.assert_not_called()
        assert "skipped_cached=7 requests=0 " in _summary(caplog_info)

    def test_max_requests_stops_and_logs_remaining(self, env, gcs, db, monkeypatch, caplog_info):
        monkeypatch.setenv(prefetch.ENV_MAX_REQUESTS, "5")
        db.cursor = _simple_db(["H1", "H2", "H3"])
        code, get, sleep = run_prefetch()
        assert code == 0
        # H1 の 7 日のうち 5 日で上限。残り 2 日と未確認の 2 ハウスは翌日以降
        assert _requested(get) == [("H1", date(2026, 10, d)) for d in (3, 4, 5, 6, 7)]
        assert gcs.list_calls == 1
        assert "carried over to the next run: days=2 (spid/house in progress) + unchecked houses=2" \
            in caplog_info.text
        summary = _summary(caplog_info)
        assert "requests=5 saved=5 " in summary and "carried_over=2 unchecked_houses=2 " in summary
        assert "stop_reason=max_requests aborted=False" in summary

    def test_max_requests_reached_exactly_at_end_is_not_carried_over(self, env, gcs, db, monkeypatch, caplog_info):
        monkeypatch.setenv(prefetch.ENV_MAX_REQUESTS, "7")
        code, get, sleep = run_prefetch()
        assert code == 0 and get.call_count == 7
        assert "carried_over=0 " in _summary(caplog_info)
        assert "stop_reason=none " in _summary(caplog_info)
        assert "carried over to the next run" not in caplog_info.text

    def test_max_requests_invalid_uses_default(self, env, gcs, db, monkeypatch):
        for raw in ("0", "x", "-3"):
            monkeypatch.setenv(prefetch.ENV_MAX_REQUESTS, raw)
            assert prefetch._env_number(prefetch.ENV_MAX_REQUESTS, 4000, int, 1) == 4000


# ---------------------------------------------------------------------------
# 失敗時（バケット未設定・DB・GCS 障害・4xx・不正レスポンス・打ち切り）
# ---------------------------------------------------------------------------
class TestFailures:
    def test_bucket_unset_exit_1_and_nothing_done(self, env, gcs, db, monkeypatch, caplog_info):
        for raw in (None, "  "):
            if raw is None:
                monkeypatch.delenv(egpf_cache.ENV_CACHE_BUCKET, raising=False)
            else:
                monkeypatch.setenv(egpf_cache.ENV_CACHE_BUCKET, raw)
            monkeypatch.setenv(egpf_cache.ENV_CACHE_ENABLED, "true")
            code, get, sleep = run_prefetch()
            assert code == 1
            get.assert_not_called()
        assert db.connect_calls == []
        assert gcs.created == []
        errors = [r for r in caplog_info.records if r.levelno == logging.ERROR]
        assert errors and "EGPF_CACHE_BUCKET is not set" in errors[0].getMessage()

    def test_db_connection_failure_exit_1(self, env, gcs, db, monkeypatch, caplog_info):
        monkeypatch.setenv(prefetch.ENV_MYSQL_CONNECT_ATTEMPTS, "2")
        db.connect_error = OSError("i/o timeout")
        code, get, sleep = run_prefetch()
        assert code == 1
        assert len(db.connect_calls) == 2
        get.assert_not_called()
        assert "Failed to load target houses from DB: OSError: i/o timeout" in caplog_info.text

    def test_gcs_list_failure_skips_house(self, env, gcs, db, caplog_info):
        db.cursor = _simple_db(["H1", "H2"])
        gcs.fail = {"list": RuntimeError("503 backend")}
        code, get, sleep = run_prefetch()
        # 1 件も保存できず一覧取得の失敗がある: バケット名・IAM の誤りを疑い終了コード 1
        assert code == 1
        get.assert_not_called()
        assert "Skip house (cannot list cache): spid=0187 house=H1" in caplog_info.text
        assert "list_failed_houses=2 " in _summary(caplog_info)
        assert "stop_reason=none " in _summary(caplog_info)
        assert "Prefetch saved nothing with GCS failures: list_failed_houses=2 save_failed=0" in caplog_info.text

    def test_gcs_client_creation_failure_does_not_crash(self, env, gcs, db, monkeypatch, caplog_info):
        gcs.inject = False
        storage_stub = MagicMock(name="google.cloud.storage")
        storage_stub.Client.side_effect = RuntimeError("no credentials")
        monkeypatch.setitem(sys.modules, "google.cloud.storage", storage_stub)
        code, get, sleep = run_prefetch()
        assert code == 1
        get.assert_not_called()
        assert storage_stub.Client.call_count == 1

    def test_gcs_upload_failure_counts_failed_and_continues(self, env, gcs, db, monkeypatch, caplog_info):
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "2")
        db.cursor = _simple_db(["H1", "H2"])
        gcs.fail = {"upload": RuntimeError("403 forbidden")}
        code, get, sleep = run_prefetch()
        # 保存が全滅（書き込み権限なし等）: 終了コード 1。保存失敗は検証 NG と別に数える
        assert code == 1
        assert get.call_count == 4
        assert "requests=4 saved=0 failed=4 invalid=0 save_failed=4 " in _summary(caplog_info)
        assert "Prefetch saved nothing with GCS failures: list_failed_houses=0 save_failed=4" in caplog_info.text

    def test_client_error_4xx_continues_and_counts_as_guard_success(self, env, gcs, db, monkeypatch, caplog_info):
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "1")
        monkeypatch.setenv("EGPF_RETRY_MAX_ATTEMPTS", "1")
        monkeypatch.setenv("EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES", "2")
        db.cursor = _simple_db(["H1", "H2", "H3", "H4"])
        # 枯渇 → 4xx（ガードはリセット）→ 枯渇 → 成功: 連続 2 回にならないので打ち切らない
        effects = [_resp(503), _resp(404), _resp(503), _auto_ok]

        def side_effect(url, headers=None, params=None, timeout=None):
            e = effects.pop(0)
            return _auto_ok(url, headers=headers, params=params, timeout=timeout) if e is _auto_ok else e

        code, get, sleep = run_prefetch(side_effect)
        assert code == 0
        assert get.call_count == 4
        assert "Prefetch failed (client error): spid=0187 house=H2" in caplog_info.text
        assert "requests=4 saved=1 failed=3 " in _summary(caplog_info)

    def test_invalid_response_not_saved(self, env, gcs, db, monkeypatch, caplog_info):
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "3")
        bodies = [_resp(200, {"data": []}), _resp(200, json_error=ValueError("not json")), None]

        def side_effect(url, headers=None, params=None, timeout=None):
            r = bodies.pop(0)
            return r if r is not None else _auto_ok(url, headers=headers, params=params, timeout=timeout)

        code, get, sleep = run_prefetch(side_effect)
        assert code == 0
        assert prefetch_saved_days(gcs, "0187", "H1") == _days(9)
        assert "Prefetch not saved (invalid response): spid=0187 house=H1 date=2026-10-07" in caplog_info.text
        assert "Prefetch failed (not JSON)" in caplog_info.text
        # 検証 NG（空レスポンス）は invalid、JSON でないものは取得失敗。どちらも保存失敗には数えない
        assert "requests=3 saved=1 failed=2 invalid=1 save_failed=0 " in _summary(caplog_info)

    def test_guard_abort_exit_1_and_stops(self, env, gcs, db, monkeypatch, caplog_info):
        monkeypatch.setenv("EGPF_RETRY_MAX_ATTEMPTS", "2")
        monkeypatch.setenv("EGPF_RETRY_WAIT_SEC", "2")
        db.cursor = _simple_db(["H1", "H2", "H3"])
        code, get, sleep = run_prefetch(lambda *a, **k: _resp(503))
        assert code == 1
        # 既定の閾値 3: 3 リクエスト（各 2 試行）で打ち切り。残りは処理しない
        assert get.call_count == 6
        assert [h for h, _ in _requested(get)] == ["H1"] * 6
        assert gcs.upload_calls == 0
        # 待ち: 再送待ち 2 秒 ×3 とリクエスト間 1 秒 ×2
        assert sorted(c.args[0] for c in sleep.call_args_list) == [1.0, 1.0, 2.0, 2.0, 2.0]
        assert "Aborted prefetch: consecutive EGPF retry exhaustion. unchecked houses=2" in caplog_info.text
        assert "stop_reason=guard aborted=True" in _summary(caplog_info)

    def test_guard_success_resets_between_exhaustions(self, env, gcs, db, monkeypatch):
        monkeypatch.setenv("EGPF_RETRY_MAX_ATTEMPTS", "1")
        monkeypatch.setenv("EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES", "2")
        effects = [_resp(503), _auto_ok, _resp(503), _auto_ok, _resp(503), _auto_ok, _resp(503)]

        def side_effect(url, headers=None, params=None, timeout=None):
            e = effects.pop(0)
            return _auto_ok(url, headers=headers, params=params, timeout=timeout) if e is _auto_ok else e

        code, get, sleep = run_prefetch(side_effect)
        assert code == 0
        assert get.call_count == 7

    def test_cached_days_do_not_reset_guard(self, env, gcs, db, monkeypatch):
        # H1 枯渇 → H2 全日キャッシュ済み（EGPF を呼ばない）→ H3 枯渇 で閾値 2 に到達
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "1")
        monkeypatch.setenv("EGPF_RETRY_MAX_ATTEMPTS", "1")
        monkeypatch.setenv("EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES", "2")
        db.cursor = _simple_db(["H1", "H2", "H3", "H4"])
        _put(gcs, "0187", "H2", date(2026, 10, 9))
        code, get, sleep = run_prefetch(lambda *a, **k: _resp(503))
        assert code == 1
        assert [h for h, _ in _requested(get)] == ["H1", "H3"]


# ---------------------------------------------------------------------------
# 異常時の終了コード（1 件も保存できず GCS の失敗がある → 1）
# ---------------------------------------------------------------------------
class TestExitCodeOnGcsFailure:
    def test_partial_save_failure_with_some_saved_is_exit_0(self, env, gcs, db, monkeypatch, caplog_info):
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "2")
        uploads = []
        db.cursor = _simple_db(["H1", "H2"])
        real_create = egpf_cache.create_cache  # gcs フィクスチャの差し替え版

        def create_cache(writable=True, logger=None):
            cache = real_create(writable=writable, logger=logger)
            real_save = cache.save

            def save(*args):
                uploads.append(args[:3])
                if len(uploads) == 1:
                    return False  # 1 件目だけ GCS 障害
                return real_save(*args)

            cache.save = save
            return cache

        monkeypatch.setattr(egpf_cache, "create_cache", create_cache)
        code, get, sleep = run_prefetch()
        assert code == 0
        assert "requests=4 saved=3 failed=1 invalid=0 save_failed=1 " in _summary(caplog_info)
        assert "Prefetch saved nothing" not in caplog_info.text

    def test_list_failure_on_some_houses_with_saved_is_exit_0(self, env, gcs, db, monkeypatch, caplog_info):
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "1")
        db.cursor = _simple_db(["H1", "H2"])
        real_create = egpf_cache.create_cache

        def create_cache(writable=True, logger=None):
            cache = real_create(writable=writable, logger=logger)
            real_list = cache.list_cached_days
            cache.list_cached_days = lambda spid, houseid: None if houseid == "H1" else real_list(spid, houseid)
            return cache

        monkeypatch.setattr(egpf_cache, "create_cache", create_cache)
        code, get, sleep = run_prefetch()
        assert code == 0
        assert "saved=1 " in _summary(caplog_info) and "list_failed_houses=1 " in _summary(caplog_info)

    def test_only_invalid_responses_saved_nothing_is_exit_0(self, env, gcs, db, monkeypatch, caplog_info):
        # 検証 NG（空レスポンス）だけで保存 0 件は GCS 障害ではないので 0
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "2")
        code, get, sleep = run_prefetch(lambda *a, **k: _resp(200, {"data": []}))
        assert code == 0
        assert "requests=2 saved=0 failed=2 invalid=2 save_failed=0 " in _summary(caplog_info)
        assert gcs.upload_calls == 0

    def test_all_cached_saved_nothing_is_exit_0(self, env, gcs, db, caplog_info):
        for d in range(3, 10):
            _put(gcs, "0187", "H1", date(2026, 10, d))
        code, get, sleep = run_prefetch()
        assert code == 0
        assert "saved=0 failed=0 invalid=0 save_failed=0 list_failed_houses=0 " in _summary(caplog_info)


# ---------------------------------------------------------------------------
# キャッシュの遮断（EgpfCache.disabled）で打ち切り
# ---------------------------------------------------------------------------
class FakeCache:
    """disabled を持つ EgpfCache のフェイク。disable_after_saves 回保存したら disabled を立てる。"""

    def __init__(self, disabled=False, disable_after_saves=None, disable_after_lists=None, list_result=None):
        self.bucket_name = BUCKET
        self.disabled = disabled
        self.disable_after_saves = disable_after_saves
        self.disable_after_lists = disable_after_lists
        self.list_result = list_result
        self.saves = []
        self.lists = []

    def list_cached_days(self, spid, houseid):
        self.lists.append(houseid)
        if self.disable_after_lists is not None and len(self.lists) >= self.disable_after_lists:
            self.disabled = True
        return None if self.list_result is None else set(self.list_result)

    def save(self, spid, houseid, day, sts, ets, response_json):
        self.saves.append((houseid, day))
        if self.disable_after_saves is not None and len(self.saves) >= self.disable_after_saves:
            self.disabled = True
            return False
        return True


@pytest.fixture
def fake_cache(monkeypatch):
    holder = types.SimpleNamespace(cache=FakeCache(list_result=()))
    monkeypatch.setattr(egpf_cache, "create_cache", lambda writable=True, logger=None: holder.cache)
    return holder


class TestCacheDisabled:
    def test_disabled_from_start_calls_nothing_and_exit_1(self, env, db, fake_cache, caplog_info):
        fake_cache.cache = FakeCache(disabled=True, list_result=())
        db.cursor = _simple_db(["H1", "H2"])
        code, get, sleep = run_prefetch()
        assert code == 1
        get.assert_not_called()
        assert fake_cache.cache.lists == []
        summary = _summary(caplog_info)
        assert "requests=0 " in summary and "unchecked_houses=2 " in summary
        assert "stop_reason=cache_disabled aborted=True" in summary
        assert "Aborted prefetch: EGPF cache is disabled" in caplog_info.text

    def test_disabled_during_saves_stops_before_next_day(self, env, db, fake_cache, caplog_info):
        # 3 件目の保存で遮断 → 4 日目以降と H2 は EGPF を呼ばない
        fake_cache.cache = FakeCache(disable_after_saves=3, list_result=())
        db.cursor = _simple_db(["H1", "H2"])
        code, get, sleep = run_prefetch()
        assert code == 1
        assert _requested(get) == [("H1", date(2026, 10, d)) for d in (3, 4, 5)]
        summary = _summary(caplog_info)
        assert "requests=3 saved=2 failed=1 invalid=0 save_failed=1 " in summary
        assert "carried_over=4 unchecked_houses=1 stop_reason=cache_disabled aborted=True" in summary

    def test_disabled_by_list_failures_stops_before_next_house(self, env, db, fake_cache, caplog_info):
        # 一覧取得の失敗で遮断（3 ハウス目で立つ）→ 4 ハウス目以降は一覧も取らない
        fake_cache.cache = FakeCache(disable_after_lists=3, list_result=None)
        db.cursor = _simple_db(["H1", "H2", "H3", "H4", "H5"])
        code, get, sleep = run_prefetch()
        assert code == 1
        get.assert_not_called()
        assert fake_cache.cache.lists == ["H1", "H2", "H3"]
        summary = _summary(caplog_info)
        assert "list_failed_houses=3 carried_over=0 unchecked_houses=2 stop_reason=cache_disabled" in summary

    def test_disabled_on_last_save_is_exit_1(self, env, db, fake_cache, monkeypatch, caplog_info):
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "2")
        fake_cache.cache = FakeCache(disable_after_saves=2, list_result=())
        code, get, sleep = run_prefetch()
        assert code == 1 and get.call_count == 2
        assert "carried_over=0 unchecked_houses=0 stop_reason=cache_disabled" in _summary(caplog_info)

    def test_cache_without_disabled_attribute_is_treated_as_enabled(self, env, db, fake_cache, caplog_info):
        cache = FakeCache(list_result=())
        del cache.disabled
        fake_cache.cache = cache
        code, get, sleep = run_prefetch()
        assert code == 0 and get.call_count == 7
        assert "stop_reason=none " in _summary(caplog_info)


# ---------------------------------------------------------------------------
# 時間による打ち切り（EGPF_PREFETCH_MAX_MINUTES）
# ---------------------------------------------------------------------------
class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.t


def _slow_ok(clock, sec):
    """1 リクエストごとに clock を sec 秒進める正常レスポンス。"""
    def side_effect(url, headers=None, params=None, timeout=None):
        clock.t += sec
        return _auto_ok(url, headers=headers, params=params, timeout=timeout)
    return side_effect


class TestMaxMinutes:
    def test_stops_after_elapsed_exceeds_and_carries_over_exit_0(self, env, gcs, db, monkeypatch, caplog_info):
        monkeypatch.setenv(prefetch.ENV_MAX_MINUTES, "3")
        db.cursor = _simple_db(["H1", "H2"])
        clock = FakeClock()
        # 1 リクエスト 60 秒: 3 件で 180 秒（超えていない）、4 件で 240 秒 → 5 件目の前で打ち切り
        code, get, sleep = run_prefetch(_slow_ok(clock, 60), clock=clock)
        assert code == 0
        assert _requested(get) == [("H1", date(2026, 10, d)) for d in (3, 4, 5, 6)]
        assert ("Reached EGPF_PREFETCH_MAX_MINUTES=3.0 (elapsed=240.0s). carried over to the next run: "
                "days=3 (spid/house in progress) + unchecked houses=1") in caplog_info.text
        summary = _summary(caplog_info)
        assert "requests=4 saved=4 " in summary
        assert "carried_over=3 unchecked_houses=1 stop_reason=max_minutes aborted=False elapsed=240.0s" in summary

    def test_checked_before_each_house(self, env, gcs, db, monkeypatch, caplog_info):
        monkeypatch.setenv(prefetch.ENV_MAX_MINUTES, "3")
        monkeypatch.setenv(prefetch.ENV_CATCHUP_DAYS, "1")
        db.cursor = _simple_db(["H1", "H2", "H3"])
        clock = FakeClock()
        code, get, sleep = run_prefetch(_slow_ok(clock, 200), clock=clock)
        assert code == 0
        assert [h for h, _ in _requested(get)] == ["H1"]
        assert gcs.list_calls == 1  # H2 は一覧も取らない
        assert "carried_over=0 unchecked_houses=2 stop_reason=max_minutes" in _summary(caplog_info)

    def test_within_limit_runs_to_end(self, env, gcs, db, monkeypatch, caplog_info):
        clock = FakeClock()
        # 既定 100 分: 1 リクエスト 60 秒 × 7 = 7 分
        code, get, sleep = run_prefetch(_slow_ok(clock, 60), clock=clock)
        assert code == 0 and get.call_count == 7
        assert "stop_reason=none " in _summary(caplog_info)
        assert "max_minutes=100.0 " in caplog_info.text

    def test_default_clock_is_monotonic(self, env, gcs, db, caplog_info):
        clock = FakeClock()
        # clock を渡さなければ time.monotonic で計測する（1 リクエストで既定の 100 分を超えさせる）
        with patch.object(time, "monotonic", clock):
            code, get, sleep = run_prefetch(_slow_ok(clock, 7000))
        assert clock.calls > 0
        assert code == 0 and get.call_count == 1
        assert "stop_reason=max_minutes" in _summary(caplog_info)

    @pytest.mark.parametrize("raw,expected", [
        ("", 100.0), ("x", 100.0), ("0", 100.0), ("-5", 100.0), ("0.5", 100.0), ("1", 1.0), ("1.5", 1.5),
        ("240", 240.0),
    ])
    def test_env_invalid_or_below_minimum_uses_default(self, monkeypatch, raw, expected):
        monkeypatch.setenv(prefetch.ENV_MAX_MINUTES, raw)
        assert prefetch._env_number(prefetch.ENV_MAX_MINUTES, prefetch.DEFAULT_MAX_MINUTES, float,
                                    prefetch.MIN_MAX_MINUTES) == expected

    def test_invalid_env_in_run_uses_default(self, env, gcs, db, monkeypatch, caplog_info):
        monkeypatch.setenv(prefetch.ENV_MAX_MINUTES, "0")
        clock = FakeClock()
        code, get, sleep = run_prefetch(_slow_ok(clock, 60), clock=clock)
        assert code == 0 and get.call_count == 7
        assert "max_minutes=100.0 " in caplog_info.text


# ---------------------------------------------------------------------------
# bin/deploy.sh（ダミー gcloud で呼び出しを記録。本物の gcloud は呼ばない）
# ---------------------------------------------------------------------------
_DEPLOY_SH = os.path.join(_BASE_DIR, "bin", "deploy.sh")
_DEPLOY_BASE_ENV = {
    "MCI_MYSQL_USER": "u", "MCI_MYSQL_PASSWORD": "p", "MCI_MYSQL_DATABASE": "d",
    "MCI_MYSQL_HOST": "10.0.0.5", "API_SHARED_PASSWORD": "apw",
}
_VPC = {"VPC_NETWORK": "default", "VPC_SUBNET": "default", "VPC_EGRESS": "private-ranges-only"}


@pytest.fixture
def deploy(tmp_path):
    """deploy.sh を tmp にコピーし（.env.* を読ませない）、PATH 先頭のダミー gcloud で実行する。"""
    import shutil
    import subprocess
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    root = tmp_path / "proj"
    (root / "bin").mkdir(parents=True)
    shutil.copy(_DEPLOY_SH, str(root / "bin" / "deploy.sh"))
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    log = tmp_path / "gcloud.log"
    gcloud = fakebin / "gcloud"
    gcloud.write_text(
        "#!%s\n"
        "import json, os, sys\n"
        "with open(os.environ['GCLOUD_LOG'], 'a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:4] == ['config', 'get-value', 'project']:\n"
        "    print('test-project')\n" % sys.executable)
    gcloud.chmod(0o755)

    def run(**extra):
        env = {"PATH": "%s:/usr/bin:/bin" % fakebin, "HOME": str(tmp_path), "GCLOUD_LOG": str(log)}
        env.update(_DEPLOY_BASE_ENV)
        env.update({k: v for k, v in extra.items() if v is not None})
        if log.exists():
            log.unlink()
        proc = subprocess.run(["bash", str(root / "bin" / "deploy.sh")], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True)
        calls = [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []
        return proc.returncode, proc.stdout, calls

    return run


def _deploy_calls(calls, job):
    return [c for c in calls if c[:4] == ["run", "jobs", "deploy", job]]


def _env_vars(call):
    value = call[call.index("--set-env-vars") + 1]
    return dict(item.split("=", 1) for item in value.split(","))


_MAIN_ENV_VARS = ("GOOGLE_CLOUD_PROJECT=test-project,MCI_MYSQL_USER=u,MCI_MYSQL_PASSWORD=p,MCI_MYSQL_HOST=10.0.0.5,"
                  "MCI_MYSQL_DATABASE=d,MCI_MYSQL_TIMEZONE=Asia/Tokyo,API_SHARED_PASSWORD=apw,LOG_LEVEL=INFO,"
                  "ERROR_NOTIFY_ENV_LABEL=STG")


class TestDeploySh:
    def test_default_target_without_bucket_is_unchanged(self, deploy):
        """DEPLOY_TARGET 未指定・バケット未設定の gcloud 呼び出しは Sprint 1 時点の deploy.sh と同じ。"""
        code, out, calls = deploy(ENV="stg")
        assert code == 0, out
        assert calls == [
            ["config", "get-value", "project"],
            ["builds", "submit", "--tag", "gcr.io/test-project/stg-mci-ver4"],
            ["run", "jobs", "deploy", "stg-mci-ver4", "--image", "gcr.io/test-project/stg-mci-ver4",
             "--region", "asia-northeast1", "--parallelism", "1", "--task-timeout", "86400s",
             "--set-env-vars", _MAIN_ENV_VARS],
        ]

    def test_main_does_not_require_vpc(self, deploy):
        code, out, calls = deploy(ENV="stg", EGPF_CACHE_BUCKET="stg-mci-egpf-cache")
        assert code == 0, out
        assert _env_vars(_deploy_calls(calls, "stg-mci-ver4")[0])["EGPF_CACHE_BUCKET"] == "stg-mci-egpf-cache"

    @pytest.mark.parametrize("target", ["main", "prefetch", "all"])
    @pytest.mark.parametrize("env_name,bucket", [
        ("prd", "stg-mci-egpf-cache"), ("stg", "prd-mci-egpf-cache"), ("stg", "stg2-mci-egpf-cache"),
        ("stg", "mci-egpf-cache"),
    ])
    def test_bucket_of_other_env_exit_1_before_build(self, deploy, target, env_name, bucket):
        code, out, calls = deploy(ENV=env_name, DEPLOY_TARGET=target, EGPF_CACHE_BUCKET=bucket, **_VPC)
        assert code == 1
        assert "EGPF_CACHE_BUCKET=%s は ENV=%s のバケットではありません" % (bucket, env_name) in out
        assert calls == [["config", "get-value", "project"]]

    @pytest.mark.parametrize("target", ["main", "prefetch", "all"])
    def test_base_date_on_prd_exit_1_before_build(self, deploy, target):
        code, out, calls = deploy(ENV="prd", DEPLOY_TARGET=target, EGPF_CACHE_BUCKET="prd-mci-egpf-cache",
                                  EGPF_PREFETCH_BASE_DATE="2026-10-05", **_VPC)
        assert code == 1
        assert "本番では EGPF_PREFETCH_BASE_DATE を設定しないでください" in out
        assert calls == [["config", "get-value", "project"]]

    def test_base_date_on_stg_is_not_put_on_job_and_warns(self, deploy):
        code, out, calls = deploy(ENV="stg", DEPLOY_TARGET="all", EGPF_CACHE_BUCKET="stg-mci-egpf-cache",
                                  EGPF_PREFETCH_BASE_DATE="2026-10-05", **_VPC)
        assert code == 0, out
        for job in ("stg-mci-ver4", "stg-mci-egpf-prefetch"):
            assert "EGPF_PREFETCH_BASE_DATE" not in _env_vars(_deploy_calls(calls, job)[0])
        assert ("gcloud run jobs execute stg-mci-egpf-prefetch --region asia-northeast1 "
                "--update-env-vars EGPF_PREFETCH_BASE_DATE=2026-10-05") in out

    @pytest.mark.parametrize("target", ["prefetch", "all"])
    @pytest.mark.parametrize("missing", ["VPC_NETWORK", "VPC_SUBNET", "VPC_EGRESS"])
    def test_prefetch_without_vpc_exit_1_before_build(self, deploy, target, missing):
        vpc = dict(_VPC)
        vpc[missing] = None
        code, out, calls = deploy(ENV="stg", DEPLOY_TARGET=target, EGPF_CACHE_BUCKET="stg-mci-egpf-cache", **vpc)
        assert code == 1
        assert "  - %s" % missing in out
        assert calls == [["config", "get-value", "project"]]

    def test_prefetch_deploy_command(self, deploy):
        code, out, calls = deploy(ENV="stg", DEPLOY_TARGET="prefetch", EGPF_CACHE_BUCKET="stg-mci-egpf-cache",
                                  EGPF_PREFETCH_MAX_MINUTES="90", **_VPC)
        assert code == 0, out
        # ビルドしない・既存イメージ前提の警告
        assert not any(c[:2] == ["builds", "submit"] for c in calls)
        assert "既存イメージに prefetch.py が入っている前提です" in out
        assert "JOB_SERVICE_ACCOUNT が未設定です" in out
        assert _deploy_calls(calls, "stg-mci-ver4") == []
        call = _deploy_calls(calls, "stg-mci-egpf-prefetch")[0]
        assert call[call.index("--network") + 1] == "default"
        assert call[call.index("--subnet") + 1] == "default"
        assert call[call.index("--vpc-egress") + 1] == "private-ranges-only"
        assert "--service-account" not in call
        env_vars = _env_vars(call)
        assert env_vars["EGPF_CACHE_BUCKET"] == "stg-mci-egpf-cache"
        assert env_vars["EGPF_PREFETCH_MAX_MINUTES"] == "90"

    def test_max_minutes_only_when_set_and_service_account(self, deploy):
        code, out, calls = deploy(ENV="prd", DEPLOY_TARGET="all", EGPF_CACHE_BUCKET="prd-mci-egpf-cache",
                                  JOB_SERVICE_ACCOUNT="sa@example.iam.gserviceaccount.com", **_VPC)
        assert code == 0, out
        assert "既存イメージに prefetch.py が入っている前提です" not in out
        assert any(c[:2] == ["builds", "submit"] for c in calls)
        call = _deploy_calls(calls, "prd-mci-egpf-prefetch")[0]
        assert call[call.index("--service-account") + 1] == "sa@example.iam.gserviceaccount.com"
        assert "EGPF_PREFETCH_MAX_MINUTES" not in _env_vars(call)
        assert _env_vars(_deploy_calls(calls, "prd-mci-ver4")[0])["EGPF_CACHE_BUCKET"] == "prd-mci-egpf-cache"


# ---------------------------------------------------------------------------
# main()（ログ設定込みの終了コード）
# ---------------------------------------------------------------------------
class TestMainEntry:
    @pytest.fixture(autouse=True)
    def restore_root_logger(self):
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        yield
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
        for handler in saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(saved_level)

    def test_main_returns_exit_code(self, env, gcs, db, monkeypatch, capsys):
        monkeypatch.delenv(egpf_cache.ENV_CACHE_BUCKET, raising=False)
        assert prefetch.main() == 1
        _, err = capsys.readouterr()
        assert "EGPF_CACHE_BUCKET is not set" in err

    def test_main_unexpected_error_exit_1(self, env, gcs, db, monkeypatch):
        monkeypatch.setattr(prefetch, "run", MagicMock(side_effect=RuntimeError("boom")))
        assert prefetch.main() == 1


def test_gcloudignore_does_not_exclude_prefetch():
    """.gcloudignore の除外パターンに prefetch.py が当たらないこと（設計書 4 章）。"""
    import fnmatch
    with open(os.path.join(_BASE_DIR, ".gcloudignore"), encoding="utf-8") as f:
        patterns = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    for p in patterns:
        assert not fnmatch.fnmatch("prefetch.py", p.rstrip("/")), p
