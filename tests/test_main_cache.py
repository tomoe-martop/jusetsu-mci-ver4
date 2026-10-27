# -*- coding: utf-8 -*-
"""main.py の EGPF キャッシュ読み書きの結合テスト（設計書 3.4・5.1・6.1・6.2）。

test_main_integration.py の FakeCursor / env フィクスチャ / run_main（重い依存のスタブ込み）と、
test_egpf_cache.py のフェイク GCS（FakeClient）を流用する。ネットワーク・DB・GCP 資格情報は不要。
GCS クライアントは egpf_cache.create_cache をラップしてフェイクを注入する（バケット未設定時は本物のまま None）。
実行: python -m pytest tests -q
"""
import sys
import types
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

import egpf_cache
from test_egpf_cache import FakeClient
from test_main_integration import (  # noqa: F401  (env / restore_root_logger はフィクスチャとして使う)
    FakeCursor,
    _house,
    _resp,
    env,
    main,
    restore_root_logger,
    run_main,
)

BUCKET = "stg-mci-egpf-cache"
DAY1 = date(2026, 8, 1)
DAY2 = date(2026, 8, 2)
RANGE_2DAYS = ("2026-08-01", "2026-08-02")
MOCK_URL = "https://mock-api-stg.example.test/0.2/estimated_data"


def _day_response(day, powers_by_type):
    """対象日の sts から 1 分刻みの timestamps を持つレスポンス。powers_by_type: {appliance_type_id: [powers]}"""
    sts, _ = egpf_cache.day_bounds(day)
    n = max(len(p) for p in powers_by_type.values())
    return {"data": [{
        "timestamps": [sts + 60 * i for i in range(n)],
        "appliance_types": [{"appliance_type_id": t, "appliances": [{"powers": p}]}
                            for t, p in powers_by_type.items()],
    }]}


# 日ごとに中身が違うレスポンス（CSV 一致の確認で取り違えを検出できるように）
RESP_DAY1 = _day_response(DAY1, {2: [1.0, 0.0, None, 3.0], 24: [None, 5.0, None, 0.0], 999: [9.0, 9.0, 9.0, 9.0]})
RESP_DAY2 = _day_response(DAY2, {5: [0.0, None, 2.5], 301: [None, None, 1.0]})


def _put(client, spid, houseid, day, response, overrides=None):
    sts, ets = egpf_cache.day_bounds(day)
    envelope = egpf_cache.build_envelope(spid, houseid, day, sts, ets, response)
    envelope.update(overrides or {})
    client.put_envelope(egpf_cache.object_key(spid, houseid, day), envelope)


@pytest.fixture
def gcs(monkeypatch):
    """create_cache が返す EgpfCache にフェイク GCS クライアントを注入する。生成した cache も記録する。"""
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
    # egpf_cache が遅延 import する google.cloud.storage（フェイク注入時は呼ばれないこと）
    storage_stub = MagicMock(name="google.cloud.storage(egpf_cache)")
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage_stub)
    for name in (egpf_cache.ENV_CACHE_BUCKET, egpf_cache.ENV_CACHE_ENABLED):
        monkeypatch.delenv(name, raising=False)
    return types.SimpleNamespace(client=client, created=created, storage_stub=storage_stub)


def _cache_on(monkeypatch, value="true"):
    monkeypatch.setenv(egpf_cache.ENV_CACHE_BUCKET, BUCKET)
    monkeypatch.setenv(egpf_cache.ENV_CACHE_ENABLED, value)


def _csv_files(env):
    """出力された予測入力 CSV を {ファイル名からタイムスタンプ部を除いたもの: 内容} で返す。"""
    out = {}
    for p in sorted(env.data_dir.glob("*.csv")):
        prefix = p.name.rsplit("_", 1)[0]  # {YYYYMMDD}_{houseid}_{ts}.csv → {YYYYMMDD}_{houseid}
        out[prefix] = p.read_text(encoding="utf-8")
    return out


def _uploaded_keys(client):
    return sorted(k for k in client.objects if k.startswith(egpf_cache.KEY_PREFIX + "/"))


# ---------------------------------------------------------------------------
# バケット未設定: 完全に現行どおり
# ---------------------------------------------------------------------------
class TestBucketUnset:
    @pytest.mark.parametrize("enabled", [None, "true"])
    def test_no_cache_object_and_same_egpf_calls(self, env, gcs, monkeypatch, capsys, enabled):
        if enabled is not None:
            monkeypatch.setenv(egpf_cache.ENV_CACHE_ENABLED, enabled)
        cursor = FakeCursor([(10,) + RANGE_2DAYS], {10: [_house(1, "H1")]})

        code, get, post, sleep = run_main(cursor, [_resp(200, RESP_DAY1), _resp(200, RESP_DAY2)])

        assert code == 0
        assert gcs.created == [None]  # create_cache は 1 回だけ呼ばれ、None
        assert get.call_count == 2
        assert gcs.storage_stub.Client.call_count == 0  # GCS クライアントを作らない
        assert gcs.client.blob_calls == 0 and gcs.client.list_calls == 0
        assert (1, 1, 100) in cursor.house_updates()
        out, err = capsys.readouterr()
        assert "EGPF cache" not in out + err


# ---------------------------------------------------------------------------
# ON（EGPF_CACHE_ENABLED=ON + バケットあり）
# ---------------------------------------------------------------------------
class TestCacheOn:
    def test_all_hit_calls_no_egpf_and_csv_matches_uncached_run(self, env, gcs, monkeypatch):
        # 1 回目: キャッシュ無し（バケット未設定）で EGPF から取得
        cursor = FakeCursor([(20,) + RANGE_2DAYS], {20: [_house(1, "H1")]})
        code, get, post, sleep = run_main(cursor, [_resp(200, RESP_DAY1), _resp(200, RESP_DAY2)])
        assert code == 0 and get.call_count == 2
        csv_uncached = _csv_files(env)
        assert list(csv_uncached) == ["20260801_H1"]

        # 2 回目: 同じレスポンスがキャッシュにあり、全日ヒット
        _put(gcs.client, "0187", "H1", DAY1, RESP_DAY1)
        _put(gcs.client, "0187", "H1", DAY2, RESP_DAY2)
        _cache_on(monkeypatch)
        cursor = FakeCursor([(21,) + RANGE_2DAYS], {21: [_house(1, "H1")]})
        code, get, post, sleep = run_main(cursor, [])

        assert code == 0
        assert get.call_count == 0
        assert gcs.client.upload_calls == 0
        assert (1, 1, 100) in cursor.house_updates()
        # 設計書 6.2: 予測入力 CSV が一致（ファイル名のタイムスタンプ部は除く）
        assert _csv_files(env) == csv_uncached

    def test_hit_skips_id_token_for_mock_spid(self, env, gcs, monkeypatch):
        monkeypatch.setenv("MOCK_API_URL", MOCK_URL)
        _cache_on(monkeypatch, " ON ")
        _put(gcs.client, "9991", "M1", DAY1, RESP_DAY1)
        cursor = FakeCursor([(22, "2026-08-01", "2026-08-01")], {22: [_house(1, "M1", spid="9991")]})

        with patch.object(main.google.oauth2.id_token, "fetch_id_token", return_value="tok") as fetch:
            code, get, post, sleep = run_main(cursor, [])

        assert code == 0
        assert get.call_count == 0
        fetch.assert_not_called()
        assert (1, 1, 100) in cursor.house_updates()

    def test_partial_hit_fetches_and_saves_only_missing_day(self, env, gcs, monkeypatch):
        _cache_on(monkeypatch, "1")
        _put(gcs.client, "0187", "H1", DAY1, RESP_DAY1)
        cursor = FakeCursor([(30,) + RANGE_2DAYS], {30: [_house(1, "H1")]})

        code, get, post, sleep = run_main(cursor, [_resp(200, RESP_DAY2)])

        assert code == 0
        assert get.call_count == 1
        sts2, ets2 = egpf_cache.day_bounds(DAY2)
        assert get.call_args.kwargs["params"]["sts"] == sts2
        assert get.call_args.kwargs["params"]["ets"] == ets2
        assert gcs.client.upload_calls == 1
        key2 = egpf_cache.object_key("0187", "H1", DAY2)
        assert _uploaded_keys(gcs.client) == sorted([egpf_cache.object_key("0187", "H1", DAY1), key2])
        # 保存された日は次回ヒットする（封筒の sts/ets/response が要求と一致）
        loaded = egpf_cache.EgpfCache(BUCKET, client=gcs.client).load("0187", "H1", DAY2, sts2, ets2)
        assert loaded == RESP_DAY2
        assert (1, 1, 100) in cursor.house_updates()

    def test_unfinished_days_are_not_saved(self, env, gcs, monkeypatch):
        """即時実行は当日・未来日を含む期間でも作れる（PR #9 レビュー指摘）。終わっていない日は保存しない。"""
        from datetime import datetime, timedelta
        _cache_on(monkeypatch)
        today = datetime.now(egpf_cache.JST).date()
        days = [today - timedelta(days=1), today, today + timedelta(days=1)]
        cursor = FakeCursor([(32, days[0].isoformat(), days[-1].isoformat())], {32: [_house(1, "H1")]})

        code, get, post, sleep = run_main(
            cursor, [_resp(200, _day_response(d, {2: [1.0, 0.0]})) for d in days])

        assert code == 0
        assert get.call_count == 3  # 3 日ともキャッシュに無いので EGPF から取得
        assert _uploaded_keys(gcs.client) == [egpf_cache.object_key("0187", "H1", days[0])]  # 前日分だけ保存
        assert (1, 1, 100) in cursor.house_updates()

    def test_miss_with_invalid_response_is_not_saved(self, env, gcs, monkeypatch):
        _cache_on(monkeypatch)
        cursor = FakeCursor([(31, "2026-08-01", "2026-08-01")], {31: [_house(1, "H1")]})

        code, get, post, sleep = run_main(cursor, [_resp(200, {"data": []})])

        assert code == 0
        assert get.call_count == 1
        assert gcs.client.upload_calls == 0
        assert (1, -1, 10) in cursor.house_updates()  # 現行と同じ失敗（データ無し）

    @pytest.mark.parametrize("fail", [
        {"download": RuntimeError("gcs read down")},
        {"upload": RuntimeError("gcs write down")},
        {"download": RuntimeError("gcs read down"), "upload": RuntimeError("gcs write down")},
    ])
    def test_gcs_failure_falls_back_to_egpf_and_house_succeeds(self, env, gcs, monkeypatch, capsys, fail):
        _cache_on(monkeypatch)
        gcs.client.fail = fail
        cursor = FakeCursor([(40,) + RANGE_2DAYS], {40: [_house(1, "H1")]})

        code, get, post, sleep = run_main(cursor, [_resp(200, RESP_DAY1), _resp(200, RESP_DAY2)])

        assert code == 0
        assert get.call_count == 2
        assert (1, 1, 100) in cursor.house_updates()
        assert cursor.results() == [(40, 1, 40)]
        out, err = capsys.readouterr()
        assert "failed (fallback to EGPF)" in out + err

    def test_client_creation_failure_falls_back_to_egpf(self, env, gcs, monkeypatch, capsys):
        _cache_on(monkeypatch)
        gcs.client.inject = False  # フェイクを注入せず、遅延生成させる
        gcs.storage_stub.Client.side_effect = RuntimeError("no credentials")
        cursor = FakeCursor([(41,) + RANGE_2DAYS], {41: [_house(1, "H1"), _house(2, "H2")]})

        code, get, post, sleep = run_main(cursor, [_resp(200, RESP_DAY1), _resp(200, RESP_DAY2)] * 2)

        assert code == 0
        assert get.call_count == 4
        assert gcs.storage_stub.Client.call_count == 1  # 生成失敗は再試行しない
        assert (1, 1, 100) in cursor.house_updates() and (2, 1, 100) in cursor.house_updates()
        out, err = capsys.readouterr()
        assert "no credentials" in out + err

    @pytest.mark.parametrize("overrides", [
        {"schema": "old-schema/v0"},
        {"sts": 0},
        {"response": {"data": []}},
    ])
    def test_invalid_cache_is_refetched_and_overwritten(self, env, gcs, monkeypatch, overrides):
        _cache_on(monkeypatch)
        _put(gcs.client, "0187", "H1", DAY1, RESP_DAY1, overrides)
        cursor = FakeCursor([(50, "2026-08-01", "2026-08-01")], {50: [_house(1, "H1")]})

        code, get, post, sleep = run_main(cursor, [_resp(200, RESP_DAY1)])

        assert code == 0
        assert get.call_count == 1
        assert gcs.client.upload_calls == 1
        sts1, ets1 = egpf_cache.day_bounds(DAY1)
        envelope = egpf_cache.EgpfCache(BUCKET, client=gcs.client).load_envelope("0187", "H1", DAY1, sts1, ets1)
        assert envelope is not None
        assert envelope["schema"] == egpf_cache.SCHEMA and envelope["response"] == RESP_DAY1
        assert (1, 1, 100) in cursor.house_updates()

    def test_cache_hit_does_not_reset_consecutive_failure_guard(self, env, gcs, monkeypatch):
        # H1 枯渇 → H2 全日ヒット（カウンタを戻さない）→ H3 枯渇 → H4 枯渇 で 3 連続とみなして打ち切り
        _cache_on(monkeypatch)
        _put(gcs.client, "0187", "H2", DAY1, RESP_DAY1)
        houses = [_house(i, "H%d" % i) for i in range(1, 6)]
        cursor = FakeCursor([(60, "2026-08-01", "2026-08-01")], {60: houses})

        code, get, post, sleep = run_main(cursor, [_resp(503)] * 100)

        assert code == 0
        assert get.call_count == 15  # H1/H3/H4 × 5 試行。H2 は EGPF を呼ばない
        assert (2, 1, 100) in cursor.house_updates()
        assert [u for u in cursor.house_updates() if u[0] == 5] == []
        assert cursor.task_ends() == [(-1, 60)]
        assert "未処理 1" in post.call_args.kwargs["json"]["text"]


# ---------------------------------------------------------------------------
# OFF + バケットあり: 現行どおり取得し、比較ログのみ（設計書 5.1）
# ---------------------------------------------------------------------------
class TestCacheOffWithBucket:
    @pytest.mark.parametrize("enabled", [None, "", "0", "off", "false"])
    def test_same_egpf_calls_no_upload_and_compare_log_per_day(self, env, gcs, monkeypatch, capsys, enabled):
        monkeypatch.setenv(egpf_cache.ENV_CACHE_BUCKET, BUCKET)
        if enabled is not None:
            monkeypatch.setenv(egpf_cache.ENV_CACHE_ENABLED, enabled)
        # DAY1 はキャッシュあり（EGPF より 1 分少ない）、DAY2 はキャッシュ無し
        cached_day1 = _day_response(DAY1, {2: [1.0, 0.0, None, None], 24: [None, 5.0, None, None]})
        _put(gcs.client, "0187", "H1", DAY1, cached_day1)
        cursor = FakeCursor([(70,) + RANGE_2DAYS], {70: [_house(1, "H1")]})

        code, get, post, sleep = run_main(cursor, [_resp(200, RESP_DAY1), _resp(200, RESP_DAY2)])

        assert code == 0
        assert get.call_count == 2  # キャッシュにある日も EGPF から取得する（現行と同じ回数）
        assert gcs.client.upload_calls == 0
        assert (1, 1, 100) in cursor.house_updates()
        out, err = capsys.readouterr()
        logs = out + err
        assert ("EGPF cache compare: spid=0187 house=H1 date=2026-08-01 "
                "cache_filled=2 egpf_filled=3 diff=1") in logs
        assert ("EGPF cache compare: spid=0187 house=H1 date=2026-08-02 "
                "cache_filled=missing egpf_filled=2 diff=-") in logs

    def test_csv_uses_egpf_response_not_cache(self, env, gcs, monkeypatch):
        monkeypatch.setenv(egpf_cache.ENV_CACHE_BUCKET, BUCKET)
        cursor = FakeCursor([(71, "2026-08-01", "2026-08-01")], {71: [_house(1, "H1")]})
        run_main(cursor, [_resp(200, RESP_DAY1)])
        expected = _csv_files(env)

        # 中身の違うキャッシュがあっても、OFF なら CSV は EGPF の値で作る
        _put(gcs.client, "0187", "H1", DAY1, _day_response(DAY1, {2: [None, None, None, 9.0]}))
        cursor = FakeCursor([(72, "2026-08-01", "2026-08-01")], {72: [_house(1, "H1")]})
        code, get, post, sleep = run_main(cursor, [_resp(200, RESP_DAY1)])

        assert code == 0 and get.call_count == 1
        assert _csv_files(env) == expected
