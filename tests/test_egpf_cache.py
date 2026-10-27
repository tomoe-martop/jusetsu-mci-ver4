# -*- coding: utf-8 -*-
"""api/egpf_cache.py の単体テスト（設計書 3.2・6.1）。

GCS はフェイククライアントで代替し、ネットワーク・GCP 資格情報は使わない。
google.cloud.storage はモジュールトップで import しない（test_main_integration.py のスタブと共存するため）。
import 失敗・Client 生成失敗は sys.modules を monkeypatch で一時的に差し替えて確認する。
実行: python -m pytest tests -q
"""
import gzip
import json
import logging
import sys
import types
from datetime import date, datetime, timedelta, timezone

import pytest

import egpf_cache
from egpf_cache import (
    APP_TYPE_IDS,
    ENV_CACHE_BUCKET,
    ENV_CACHE_ENABLED,
    GCS_MAX_CONSECUTIVE_FAILURES,
    GCS_RETRY_DEADLINE_SEC,
    GCS_TIMEOUT_SEC,
    JST,
    KEY_PREFIX,
    SCHEMA,
    EgpfCache,
    build_envelope,
    count_filled_minutes,
    create_cache,
    day_bounds,
    get_bucket_name,
    is_cache_enabled,
    is_valid_response,
    object_key,
)

BUCKET = "stg-mci-egpf-cache"
SPID = "0187"
HOUSE = "MAEBASHI02"
DAY = date(2026, 10, 1)
STS, ETS = day_bounds(DAY)
NOW = ETS + 5 * 3600  # 翌日 5:00 JST（事前取得ジョブの実行時刻相当。DAY は確定済み）
LOGGER_NAME = "test_egpf_cache"


# ---------------------------------------------------------------------------
# フェイク GCS
# ---------------------------------------------------------------------------
class NotFound(Exception):
    """google.api_core.exceptions.NotFound の代わり（クラス名で判定される）。"""
    code = 404


class FakeBlob:
    def __init__(self, store, name, fail=None):
        self._store = store
        self.name = name
        self._fail = fail or {}
        self.metadata = None
        self.content_encoding = None
        self.content_type = None

    def download_as_bytes(self, **kwargs):
        self._store.calls.append(("download", kwargs))
        if "download" in self._fail:
            raise self._fail["download"]
        if self.name not in self._store.objects:
            raise NotFound("No such object: %s" % self.name)
        return self._store.objects[self.name]["body"]

    def upload_from_string(self, data, content_type=None, **kwargs):
        self._store.upload_calls += 1
        self._store.calls.append(("upload", kwargs))
        if "upload" in self._fail:
            raise self._fail["upload"]
        self.content_type = content_type
        self._store.objects[self.name] = {
            "body": data,
            "content_type": content_type,
            "content_encoding": self.content_encoding,
            "metadata": dict(self.metadata or {}),
        }


class FakeBucket:
    def __init__(self, store, fail):
        self._store = store
        self._fail = fail

    def blob(self, name):
        self._store.blob_calls += 1
        return FakeBlob(self._store, name, self._fail)


class FakeClient:
    def __init__(self, fail=None):
        self.objects = {}
        self.fail = fail or {}
        self.bucket_calls = []
        self.blob_calls = 0
        self.upload_calls = 0
        self.list_calls = 0
        self.calls = []  # GCS 呼び出し（download/upload/list）と渡されたキーワード引数（timeout・retry など）

    def bucket(self, name):
        self.bucket_calls.append(name)
        return FakeBucket(self, self.fail)

    def list_blobs(self, bucket_name, prefix=None, **kwargs):
        self.list_calls += 1
        self.calls.append(("list", kwargs))
        if "list" in self.fail:
            raise self.fail["list"]
        return [types.SimpleNamespace(name=n) for n in sorted(self.objects) if n.startswith(prefix or "")]

    def put_raw(self, key, body):
        self.objects[key] = {"body": body, "content_type": None, "content_encoding": None, "metadata": {}}

    def put_envelope(self, key, envelope):
        self.put_raw(key, gzip.compress(json.dumps(envelope).encode("utf-8")))


def _response(n=3, sts=STS, appliance_types=None):
    if appliance_types is None:
        appliance_types = [
            {"appliance_type_id": 2, "appliances": [{"powers": [1.0, 0.0, None][:n] + [None] * max(0, n - 3)}]},
        ]
    return {"data": [{"timestamps": [sts + 60 * i for i in range(n)], "appliance_types": appliance_types}]}


@pytest.fixture
def logger():
    lg = logging.getLogger(LOGGER_NAME)
    lg.setLevel(logging.DEBUG)
    return lg


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def cache(client, logger):
    return EgpfCache(BUCKET, client=client, logger=logger, clock=lambda: NOW)


@pytest.fixture
def env_clean(monkeypatch):
    monkeypatch.delenv(ENV_CACHE_BUCKET, raising=False)
    monkeypatch.delenv(ENV_CACHE_ENABLED, raising=False)
    return monkeypatch


def _warnings(caplog):
    return [r for r in caplog.records if r.name == LOGGER_NAME and r.levelno == logging.WARNING]


def _infos(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME and r.levelno == logging.INFO]


# ---------------------------------------------------------------------------
# 定数・キー・日付
# ---------------------------------------------------------------------------
class TestKeyAndBounds:
    def test_constants(self):
        assert egpf_cache.ENV_CACHE_ENABLED == "EGPF_CACHE_ENABLED"
        assert egpf_cache.ENV_CACHE_BUCKET == "EGPF_CACHE_BUCKET"
        assert KEY_PREFIX == "estimated_data/v1"
        assert APP_TYPE_IDS == (2, 5, 20, 24, 25, 30, 31, 37, 301)
        assert JST.utcoffset(None) == timedelta(hours=9)

    def test_object_key(self):
        assert object_key("0187", "MAEBASHI02", date(2026, 1, 5)) == \
            "estimated_data/v1/0187/MAEBASHI02/20260105.json.gz"
        assert object_key(9991, 123, date(2026, 12, 31)) == "estimated_data/v1/9991/123/20261231.json.gz"

    def test_day_bounds_jst_midnight(self):
        sts, ets = day_bounds(date(2026, 10, 1))
        assert sts == int(datetime(2026, 10, 1, tzinfo=JST).timestamp())
        # JST 0:00 = 前日 UTC 15:00
        assert datetime.fromtimestamp(sts, tz=timezone.utc) == datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
        assert ets - sts == 86400

    def test_day_bounds_matches_main_py(self):
        # main.py と同じ組み立て（"%Y-%m-%d 00:00:00+0900"）と一致すること
        start = datetime.strptime("2026-03-01 00:00:00+0900", "%Y-%m-%d %H:%M:%S%z")
        assert day_bounds(date(2026, 3, 1)) == (int(start.timestamp()), int((start + timedelta(days=1)).timestamp()))


# ---------------------------------------------------------------------------
# 環境変数
# ---------------------------------------------------------------------------
class TestEnv:
    def test_bucket_unset_or_blank(self, env_clean):
        assert get_bucket_name() is None
        env_clean.setenv(ENV_CACHE_BUCKET, "   ")
        assert get_bucket_name() is None
        env_clean.setenv(ENV_CACHE_BUCKET, " stg-mci-egpf-cache ")
        assert get_bucket_name() == "stg-mci-egpf-cache"

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "On", "on\n"])
    def test_enabled_true_values(self, env_clean, value):
        env_clean.setenv(ENV_CACHE_BUCKET, BUCKET)
        env_clean.setenv(ENV_CACHE_ENABLED, value)
        assert is_cache_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "enabled", "2"])
    def test_enabled_false_values(self, env_clean, value):
        env_clean.setenv(ENV_CACHE_BUCKET, BUCKET)
        env_clean.setenv(ENV_CACHE_ENABLED, value)
        assert is_cache_enabled() is False

    def test_enabled_unset(self, env_clean):
        env_clean.setenv(ENV_CACHE_BUCKET, BUCKET)
        assert is_cache_enabled() is False

    def test_enabled_requires_bucket(self, env_clean):
        env_clean.setenv(ENV_CACHE_ENABLED, "1")
        assert is_cache_enabled() is False
        env_clean.setenv(ENV_CACHE_BUCKET, " ")
        assert is_cache_enabled() is False

    def test_create_cache_none_without_bucket(self, env_clean):
        env_clean.setenv(ENV_CACHE_ENABLED, "1")
        assert create_cache() is None

    def test_create_cache_does_not_create_client(self, env_clean):
        env_clean.setenv(ENV_CACHE_BUCKET, BUCKET)
        calls = []
        fake_storage = types.ModuleType("google.cloud.storage")
        fake_storage.Client = lambda *a, **k: calls.append(1)
        env_clean.setitem(sys.modules, "google.cloud.storage", fake_storage)
        c = create_cache(writable=False)
        assert isinstance(c, EgpfCache)
        assert c.bucket_name == BUCKET
        assert c.writable is False
        assert calls == []
        assert create_cache().writable is True

    @pytest.mark.parametrize("enabled", [None, "", "0", "off", "false"])
    def test_create_cache_works_while_flag_off(self, env_clean, client, caplog, enabled):
        """EGPF_CACHE_ENABLED は「予測時にキャッシュを使うか」だけを切り替える（設計書 3.7・5.1・6.4）。

        フラグ OFF の期間も、バケットが設定されていれば create_cache はキャッシュを返し、
        事前取得ジョブの保存と予測実行時の比較ログが動くこと（回帰テスト）。
        """
        env_clean.setenv(ENV_CACHE_BUCKET, BUCKET)
        if enabled is not None:
            env_clean.setenv(ENV_CACHE_ENABLED, enabled)
        assert is_cache_enabled() is False
        c = create_cache(writable=True, logger=logging.getLogger(LOGGER_NAME))
        assert isinstance(c, EgpfCache)
        c._client = client  # GCS はフェイク
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is True
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        c.compare_and_log(SPID, HOUSE, DAY, STS, ETS, _response(3))
        assert any("EGPF cache compare:" in r.getMessage() and "cache_filled=2" in r.getMessage()
                   for r in caplog.records)


# ---------------------------------------------------------------------------
# filled_minutes / レスポンス検証 / 封筒
# ---------------------------------------------------------------------------
class TestFilledMinutes:
    def test_counts_any_non_null_including_zero(self):
        res = {"data": [{"timestamps": [0, 60, 120, 180], "appliance_types": [
            {"appliance_type_id": 2, "appliances": [{"powers": [None, 0.0, None, None]}]},
            {"appliance_type_id": 5, "appliances": [{"powers": [1.5, None, None, None]}]},
            {"appliance_type_id": 301, "appliances": [{"powers": [2.0, 3.0, None, None]}]},
        ]}]}
        # 0分: 5/301、1分: 2(0.0)/301、2分・3分: 全て null
        assert count_filled_minutes(res) == 2

    def test_non_target_appliance_ignored(self):
        res = {"data": [{"timestamps": [0, 60], "appliance_types": [
            {"appliance_type_id": 999, "appliances": [{"powers": [1.0, 1.0]}]},
            {"appliance_type_id": 3, "appliances": [{"powers": [1.0, 1.0]}]},
            {"appliance_type_id": 20, "appliances": [{"powers": [None, 0.0]}]},
        ]}]}
        assert count_filled_minutes(res) == 1

    def test_all_target_types_and_string_id(self):
        for type_id in APP_TYPE_IDS:
            res = {"data": [{"timestamps": [0], "appliance_types": [
                {"appliance_type_id": str(type_id), "appliances": [{"powers": [0.0]}]}]}]}
            assert count_filled_minutes(res) == 1

    def test_only_first_appliance_is_used(self):
        # main.py は appliances[0] だけを見る
        res = {"data": [{"timestamps": [0], "appliance_types": [
            {"appliance_type_id": 2, "appliances": [{"powers": [None]}, {"powers": [1.0]}]}]}]}
        assert count_filled_minutes(res) == 0

    def test_malformed_parts_are_skipped(self):
        res = {"data": [{"timestamps": [0, 60, 120], "appliance_types": [
            {"appliance_type_id": "x", "appliances": [{"powers": [1.0, 1.0, 1.0]}]},
            {"appliance_type_id": 2},
            {"appliance_type_id": 5, "appliances": []},
            {"appliance_type_id": 20, "appliances": [{"powers": ["abc", 1.0]}]},  # 短い・数値でない
        ]}]}
        assert count_filled_minutes(res) == 1

    @pytest.mark.parametrize("res", [None, {}, {"data": []}, {"data": [{}]}, {"data": [{"timestamps": None}]},
                                     "text", {"data": [{"timestamps": [0], "appliance_types": None}]}])
    def test_broken_response_is_zero(self, res):
        assert count_filled_minutes(res) == 0


class TestValidResponse:
    def test_valid_counts(self):
        assert is_valid_response(_response(1), STS, ETS) is True
        assert is_valid_response(_response(3), STS, ETS) is True
        assert is_valid_response({"data": [{"timestamps": list(range(1440))}]}, STS, ETS) is True

    def test_zero_timestamps_is_invalid(self):
        # データが無い日（mock-api-stg・未来日は timestamps: []）は保存もヒットもさせない
        assert is_valid_response(_response(0), STS, ETS) is False
        assert is_valid_response({"data": [{"timestamps": []}]}, STS, ETS) is False

    def test_upper_limit_includes_ets_minute(self):
        # 実 EGPF が ets ちょうどの分を含めて返す場合に備え、(ets-sts)/60 + 1 = 1441 件まで有効
        assert is_valid_response({"data": [{"timestamps": list(range(1441))}]}, STS, ETS) is True

    def test_too_many_timestamps(self):
        assert is_valid_response({"data": [{"timestamps": list(range(1442))}]}, STS, ETS) is False

    @pytest.mark.parametrize("res", [None, [], {}, {"data": []}, {"data": None}, {"data": ["x"]},
                                     {"data": [{}]}, {"data": [{"timestamps": "abc"}]}])
    def test_invalid_shapes(self, res):
        assert is_valid_response(res, STS, ETS) is False


class TestEnvelope:
    def test_build_envelope(self):
        res = _response(3)
        env = build_envelope(SPID, HOUSE, DAY, STS, ETS, res,
                             fetched_at=datetime(2026, 10, 2, 5, 0, 1, tzinfo=JST))
        assert env == {
            "schema": SCHEMA, "spid": SPID, "house": HOUSE, "date": "2026-10-01",
            "sts": STS, "ets": ETS, "fetched_at": "2026-10-02T05:00:01+09:00",
            "filled_minutes": 2, "response": res,
        }

    def test_fetched_at_default_is_now_jst(self):
        env = build_envelope(9991, 1, DAY, STS, ETS, _response(1))
        assert env["spid"] == "9991" and env["house"] == "1"
        assert env["fetched_at"].endswith("+09:00")
        assert abs(datetime.fromisoformat(env["fetched_at"]) - datetime.now(JST)) < timedelta(minutes=1)


# ---------------------------------------------------------------------------
# 保存・読み出し
# ---------------------------------------------------------------------------
class TestSaveLoad:
    def test_roundtrip(self, cache, client):
        res = _response(3)
        assert cache.save(SPID, HOUSE, DAY, STS, ETS, res) is True
        assert cache.load(SPID, HOUSE, DAY, STS, ETS) == res
        env = cache.load_envelope(SPID, HOUSE, DAY, STS, ETS)
        assert env["schema"] == SCHEMA and env["filled_minutes"] == 2 and env["date"] == "2026-10-01"
        assert client.bucket_calls and set(client.bucket_calls) == {BUCKET}

    def test_object_shape(self, cache, client):
        cache.save(SPID, HOUSE, DAY, STS, ETS, _response(3))
        key = "estimated_data/v1/0187/MAEBASHI02/20261001.json.gz"
        assert list(client.objects) == [key]
        obj = client.objects[key]
        assert obj["content_type"] == "application/gzip"
        assert obj["content_encoding"] is None
        assert obj["body"][:2] == b"\x1f\x8b"  # gzip
        env = json.loads(gzip.decompress(obj["body"]).decode("utf-8"))
        assert set(env) == {"schema", "spid", "house", "date", "sts", "ets", "fetched_at", "filled_minutes",
                            "response"}
        assert obj["metadata"] == {"fetched_at": env["fetched_at"], "filled_minutes": "2"}

    def test_overwrite(self, cache, client):
        cache.save(SPID, HOUSE, DAY, STS, ETS, _response(1))
        cache.save(SPID, HOUSE, DAY, STS, ETS, _response(3))
        assert cache.load(SPID, HOUSE, DAY, STS, ETS) == _response(3)

    def test_miss_returns_none_without_warning(self, cache, caplog):
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        assert cache.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert cache.load_envelope(SPID, HOUSE, DAY, STS, ETS) is None
        assert _warnings(caplog) == []

    def test_other_day_or_house_is_miss(self, cache):
        cache.save(SPID, HOUSE, DAY, STS, ETS, _response(3))
        nxt = DAY + timedelta(days=1)
        assert cache.load(SPID, HOUSE, nxt, *day_bounds(nxt)) is None
        assert cache.load(SPID, "OTHER", DAY, STS, ETS) is None
        assert cache.load("9991", HOUSE, DAY, STS, ETS) is None

    @pytest.mark.parametrize("res,count", [
        ({"data": []}, "None"), ({}, "None"), (None, "None"),
        ({"data": [{"timestamps": []}]}, "0"),
        ({"data": [{"timestamps": list(range(1442))}]}, "1442"),
    ])
    def test_invalid_response_not_saved(self, cache, client, caplog, res, count):
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        assert cache.save(SPID, HOUSE, DAY, STS, ETS, res) is False
        assert client.objects == {} and client.upload_calls == 0
        assert len(_warnings(caplog)) == 1
        # STG で実件数を確認できるよう、件数を出す
        assert "timestamps=%s" % count in _warnings(caplog)[0].getMessage()

    def test_1441_timestamps_saved(self, cache, client):
        res = {"data": [{"timestamps": [STS + 60 * i for i in range(1441)], "appliance_types": []}]}
        assert cache.save(SPID, HOUSE, DAY, STS, ETS, res) is True
        assert cache.load(SPID, HOUSE, DAY, STS, ETS) == res

    def test_saved_log_has_counts(self, cache, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        assert cache.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is True
        assert _infos(caplog) == [
            "EGPF cache saved: key=estimated_data/v1/0187/MAEBASHI02/20261001.json.gz timestamps=3 filled_minutes=2"]

    def test_read_only_never_touches_gcs_on_save(self, client, logger):
        ro = EgpfCache(BUCKET, client=client, writable=False, logger=logger, clock=lambda: NOW)
        assert ro.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is False
        assert client.bucket_calls == [] and client.blob_calls == 0 and client.upload_calls == 0
        assert client.objects == {}

    def test_read_only_never_creates_client_on_save(self, monkeypatch, logger):
        calls = []
        fake_storage = types.ModuleType("google.cloud.storage")
        fake_storage.Client = lambda *a, **k: calls.append(1)
        monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)
        ro = EgpfCache(BUCKET, writable=False, logger=logger)
        assert ro.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is False
        assert calls == []

    def test_read_only_can_load(self, client, logger):
        EgpfCache(BUCKET, client=client, logger=logger).save(SPID, HOUSE, DAY, STS, ETS, _response(3))
        ro = EgpfCache(BUCKET, client=client, writable=False, logger=logger)
        assert ro.load(SPID, HOUSE, DAY, STS, ETS) == _response(3)


# ---------------------------------------------------------------------------
# 不正なキャッシュの破棄
# ---------------------------------------------------------------------------
class TestInvalidCache:
    KEY = "estimated_data/v1/0187/MAEBASHI02/20261001.json.gz"

    def _good(self):
        return build_envelope(SPID, HOUSE, DAY, STS, ETS, _response(3))

    def _assert_discarded(self, cache, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        assert cache.load_envelope(SPID, HOUSE, DAY, STS, ETS) is None
        assert cache.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert len(_warnings(caplog)) == 2

    def test_schema_mismatch(self, cache, client, caplog):
        env = self._good()
        env["schema"] = "mci-egpf-estimated-data/v0"
        client.put_envelope(self.KEY, env)
        self._assert_discarded(cache, caplog)

    def test_schema_missing(self, cache, client, caplog):
        env = self._good()
        del env["schema"]
        client.put_envelope(self.KEY, env)
        self._assert_discarded(cache, caplog)

    def test_sts_mismatch(self, cache, client, caplog):
        env = self._good()
        env["sts"] = STS - 86400
        client.put_envelope(self.KEY, env)
        self._assert_discarded(cache, caplog)

    def test_ets_mismatch(self, cache, client, caplog):
        env = self._good()
        env["ets"] = ETS + 60
        client.put_envelope(self.KEY, env)
        self._assert_discarded(cache, caplog)

    def test_timestamps_over_limit(self, cache, client, caplog):
        env = self._good()
        env["response"] = {"data": [{"timestamps": list(range(1442)), "appliance_types": []}]}
        client.put_envelope(self.KEY, env)
        self._assert_discarded(cache, caplog)

    def test_zero_timestamps(self, cache, client, caplog):
        # 修正前に保存された空の日もヒットさせない（キャッシュ無し扱い → EGPF から取り直す）
        env = self._good()
        env["response"] = {"data": [{"timestamps": [], "appliance_types": []}]}
        client.put_envelope(self.KEY, env)
        self._assert_discarded(cache, caplog)
        assert "timestamps=0" in _warnings(caplog)[0].getMessage()

    def test_data_empty(self, cache, client, caplog):
        env = self._good()
        env["response"] = {"data": []}
        client.put_envelope(self.KEY, env)
        self._assert_discarded(cache, caplog)

    def test_response_missing(self, cache, client, caplog):
        env = self._good()
        del env["response"]
        client.put_envelope(self.KEY, env)
        self._assert_discarded(cache, caplog)

    def test_broken_gzip(self, cache, client, caplog):
        client.put_raw(self.KEY, b"\x1f\x8b\x08\x00broken")
        self._assert_discarded(cache, caplog)

    def test_not_gzip(self, cache, client, caplog):
        client.put_raw(self.KEY, json.dumps(self._good()).encode("utf-8"))
        self._assert_discarded(cache, caplog)

    def test_broken_json(self, cache, client, caplog):
        client.put_raw(self.KEY, gzip.compress(b"{not json"))
        self._assert_discarded(cache, caplog)

    def test_json_not_object(self, cache, client, caplog):
        client.put_raw(self.KEY, gzip.compress(b"[1, 2]"))
        self._assert_discarded(cache, caplog)

    def test_invalid_cache_can_be_overwritten(self, cache, client):
        client.put_raw(self.KEY, b"broken")
        assert cache.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert cache.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is True
        assert cache.load(SPID, HOUSE, DAY, STS, ETS) == _response(3)


# ---------------------------------------------------------------------------
# 一覧
# ---------------------------------------------------------------------------
class TestListCachedDays:
    def test_lists_dates_for_house(self, cache, client):
        for d in (date(2026, 9, 28), date(2026, 10, 1)):
            cache.save(SPID, HOUSE, d, *day_bounds(d), _response(1, sts=day_bounds(d)[0]))
        # 別ハウス（前方一致する名前を含む）・別 spid・関係ないオブジェクトは含めない
        cache.save(SPID, HOUSE + "X", DAY, STS, ETS, _response(1))
        cache.save("9991", HOUSE, DAY, STS, ETS, _response(1))
        client.put_raw("estimated_data/v1/0187/MAEBASHI02/readme.txt", b"")
        client.put_raw("estimated_data/v1/0187/MAEBASHI02/2026xx01.json.gz", b"")
        assert cache.list_cached_days(SPID, HOUSE) == {date(2026, 9, 28), date(2026, 10, 1)}

    def test_empty(self, cache):
        assert cache.list_cached_days(SPID, HOUSE) == set()

    def test_read_only_can_list(self, client, logger):
        ro = EgpfCache(BUCKET, client=client, writable=False, logger=logger)
        assert ro.list_cached_days(SPID, HOUSE) == set()


# ---------------------------------------------------------------------------
# GCS 障害
# ---------------------------------------------------------------------------
class TestGcsFailure:
    def _cache(self, logger, **fail):
        return EgpfCache(BUCKET, client=FakeClient(fail=fail), logger=logger, clock=lambda: NOW)

    def test_download_error(self, logger, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        c = self._cache(logger, download=RuntimeError("503 Service Unavailable"))
        assert c.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert c.load_envelope(SPID, HOUSE, DAY, STS, ETS) is None
        assert len(_warnings(caplog)) == 2
        assert "503" in _warnings(caplog)[0].getMessage()

    def test_upload_error(self, logger, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        c = self._cache(logger, upload=OSError("connection reset"))
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is False
        assert len(_warnings(caplog)) == 1

    def test_list_error(self, logger, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        c = self._cache(logger, list=RuntimeError("403 Forbidden"))
        assert c.list_cached_days(SPID, HOUSE) is None
        assert len(_warnings(caplog)) == 1

    def test_client_creation_error(self, monkeypatch, logger, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise RuntimeError("DefaultCredentialsError")

        fake_storage = types.ModuleType("google.cloud.storage")
        fake_storage.Client = boom
        monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)
        c = EgpfCache(BUCKET, logger=logger, clock=lambda: NOW)
        assert calls == []  # 生成は初回利用時
        assert c.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is False
        assert c.list_cached_days(SPID, HOUSE) is None
        # Client 生成失敗も障害に数える → 3回で遮断。以後の compare_and_log は警告を出さず missing
        assert c.disabled is True
        c.compare_and_log(SPID, HOUSE, DAY, STS, ETS, _response(3))
        assert len(_warnings(caplog)) == 4  # 障害 3 + 遮断 1
        assert "disabled" in _warnings(caplog)[3].getMessage()
        assert calls == [1]  # 失敗後は再生成しない

    def test_import_error(self, monkeypatch, logger, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        monkeypatch.setitem(sys.modules, "google.cloud.storage", None)  # import が ImportError になる
        c = EgpfCache(BUCKET, logger=logger, clock=lambda: NOW)
        assert c.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is False
        assert c.list_cached_days(SPID, HOUSE) is None
        assert len(_warnings(caplog)) == 4  # 障害 3 + 遮断 1
        assert c.disabled is True
        assert "ImportError" in _warnings(caplog)[0].getMessage() or \
            "ModuleNotFoundError" in _warnings(caplog)[0].getMessage()

    def test_lazy_client_from_storage_module(self, monkeypatch, logger):
        fake = FakeClient()
        calls = []
        fake_storage = types.ModuleType("google.cloud.storage")
        fake_storage.Client = lambda *a, **k: calls.append(1) or fake
        monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)
        c = EgpfCache(BUCKET, logger=logger, clock=lambda: NOW)
        assert calls == []
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is True
        assert c.load(SPID, HOUSE, DAY, STS, ETS) == _response(3)
        assert calls == [1]  # 2回目以降は使い回す


# ---------------------------------------------------------------------------
# 比較ログ（設計書 5.1）
# ---------------------------------------------------------------------------
class TestCompareAndLog:
    def _info(self, caplog):
        return [r.getMessage() for r in caplog.records
                if r.name == LOGGER_NAME and r.levelno == logging.INFO and "EGPF cache compare" in r.getMessage()]

    def test_same(self, cache, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        cache.save(SPID, HOUSE, DAY, STS, ETS, _response(3))
        cache.compare_and_log(SPID, HOUSE, DAY, STS, ETS, _response(3))
        assert self._info(caplog) == [
            "EGPF cache compare: spid=0187 house=MAEBASHI02 date=2026-10-01 cache_filled=2 egpf_filled=2 diff=0"]

    def test_diff(self, cache, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        cache.save(SPID, HOUSE, DAY, STS, ETS, _response(3))
        egpf = _response(3, appliance_types=[
            {"appliance_type_id": 2, "appliances": [{"powers": [1.0, 0.0, 0.0]}]}])
        cache.compare_and_log(SPID, HOUSE, DAY, STS, ETS, egpf)
        assert self._info(caplog) == [
            "EGPF cache compare: spid=0187 house=MAEBASHI02 date=2026-10-01 cache_filled=2 egpf_filled=3 diff=1"]

    def test_missing(self, cache, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        cache.compare_and_log(SPID, HOUSE, DAY, STS, ETS, _response(3))
        assert self._info(caplog) == [
            "EGPF cache compare: spid=0187 house=MAEBASHI02 date=2026-10-01 cache_filled=missing egpf_filled=2 diff=-"]

    def test_invalid_cache_is_missing(self, cache, client, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        client.put_raw(object_key(SPID, HOUSE, DAY), b"broken")
        cache.compare_and_log(SPID, HOUSE, DAY, STS, ETS, _response(3))
        assert "cache_filled=missing" in self._info(caplog)[0]

    def test_gcs_error_is_missing_and_no_exception(self, logger, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        c = EgpfCache(BUCKET, client=FakeClient(fail={"download": RuntimeError("down")}), logger=logger,
                      clock=lambda: NOW)
        c.compare_and_log(SPID, HOUSE, DAY, STS, ETS, _response(3))
        assert "cache_filled=missing" in self._info(caplog)[0]

    def test_never_raises_and_does_not_write(self, cache, client):
        cache.compare_and_log(SPID, HOUSE, DAY, STS, ETS, None)
        cache.compare_and_log(SPID, HOUSE, "not a date", STS, ETS, _response(3))
        assert client.upload_calls == 0


# ---------------------------------------------------------------------------
# 未確定の日（当日・未来日）は保存しない
# ---------------------------------------------------------------------------
class TestUnfinishedDay:
    def _cache(self, client, logger, now):
        return EgpfCache(BUCKET, client=client, logger=logger, clock=lambda: now)

    def test_today_not_saved(self, client, logger, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        c = self._cache(client, logger, STS + 12 * 3600)  # 対象日の 12:00（途中まで）
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is False
        assert client.objects == {} and client.calls == [] and client.blob_calls == 0
        infos = _infos(caplog)
        assert len(infos) == 1 and "EGPF cache save skipped (day not finished)" in infos[0]
        assert "date=2026-10-01" in infos[0] and "ets=%d" % ETS in infos[0]
        assert _warnings(caplog) == []

    def test_future_day_with_empty_response_is_info_not_warning(self, client, logger, caplog):
        # 未来日は timestamps: [] になるが、不正レスポンスの警告ではなく未確定日として INFO で弾く
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        c = self._cache(client, logger, STS - 86400)
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(0)) is False
        assert client.calls == []
        assert _warnings(caplog) == []
        assert any("day not finished" in m for m in _infos(caplog))

    def test_one_second_before_end_not_saved(self, client, logger):
        assert self._cache(client, logger, ETS - 1).save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is False
        assert client.objects == {}

    def test_ets_equal_now_is_saved(self, client, logger):
        # 猶予時間は設けない（ets <= now なら保存可）
        c = self._cache(client, logger, ETS)
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is True
        assert c.load(SPID, HOUSE, DAY, STS, ETS) == _response(3)

    def test_default_clock_is_current_time(self, client, logger):
        c = EgpfCache(BUCKET, client=client, logger=logger)
        today = datetime.now(JST).date()
        assert c.save(SPID, HOUSE, today, *day_bounds(today), _response(1, sts=day_bounds(today)[0])) is False
        yesterday = today - timedelta(days=1)
        assert c.save(SPID, HOUSE, yesterday, *day_bounds(yesterday),
                      _response(1, sts=day_bounds(yesterday)[0])) is True

    def test_broken_clock_does_not_raise(self, client, logger):
        def boom():
            raise RuntimeError("clock")
        c = EgpfCache(BUCKET, client=client, logger=logger, clock=boom)
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is False
        assert client.calls == []


# ---------------------------------------------------------------------------
# GCS 呼び出しの timeout・retry
# ---------------------------------------------------------------------------
class _FakeRetry:
    def __init__(self, name, deadline=None):
        self.name = name
        self.deadline = deadline


def _fake_retry_module(with_timeout=True, with_deadline=True):
    base = types.SimpleNamespace()
    if with_timeout:
        base.with_timeout = lambda t: _FakeRetry("with_timeout", t)
    if with_deadline:
        base.with_deadline = lambda t: _FakeRetry("with_deadline", t)
    mod = types.ModuleType("google.cloud.storage.retry")
    mod.DEFAULT_RETRY = base
    return mod


class TestGcsCallOptions:
    def _exercise(self, client, logger):
        c = EgpfCache(BUCKET, client=client, logger=logger, clock=lambda: NOW)
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is True
        assert c.load(SPID, HOUSE, DAY, STS, ETS) == _response(3)
        assert c.list_cached_days(SPID, HOUSE) == {DAY}
        assert [name for name, _ in client.calls] == ["upload", "download", "list"]
        return [kw for _, kw in client.calls]

    def test_constants(self):
        assert GCS_TIMEOUT_SEC == 5
        assert GCS_RETRY_DEADLINE_SEC == 10
        assert GCS_MAX_CONSECUTIVE_FAILURES == 3

    def test_timeout_and_retry_with_timeout(self, monkeypatch, client, logger):
        monkeypatch.setitem(sys.modules, "google.cloud.storage.retry", _fake_retry_module())
        kwargs_list = self._exercise(client, logger)
        for kw in kwargs_list:
            assert kw["timeout"] == GCS_TIMEOUT_SEC
            assert kw["retry"].name == "with_timeout" and kw["retry"].deadline == GCS_RETRY_DEADLINE_SEC
        # retry は 1度だけ組み立てて使い回す
        assert len({id(kw["retry"]) for kw in kwargs_list}) == 1

    def test_retry_with_deadline_for_old_api_core(self, monkeypatch, client, logger):
        monkeypatch.setitem(sys.modules, "google.cloud.storage.retry", _fake_retry_module(with_timeout=False))
        for kw in self._exercise(client, logger):
            assert kw["timeout"] == GCS_TIMEOUT_SEC
            assert kw["retry"].name == "with_deadline" and kw["retry"].deadline == GCS_RETRY_DEADLINE_SEC

    def test_retry_import_failure_passes_timeout_only(self, monkeypatch, client, logger):
        monkeypatch.setitem(sys.modules, "google.cloud.storage.retry", None)  # import が ImportError になる
        for kw in self._exercise(client, logger):
            assert kw == {"timeout": GCS_TIMEOUT_SEC}

    def test_retry_build_failure_passes_timeout_only(self, monkeypatch, client, logger):
        monkeypatch.setitem(sys.modules, "google.cloud.storage.retry",
                            _fake_retry_module(with_timeout=False, with_deadline=False))
        for kw in self._exercise(client, logger):
            assert kw == {"timeout": GCS_TIMEOUT_SEC}


# ---------------------------------------------------------------------------
# 連続障害での遮断
# ---------------------------------------------------------------------------
class TestCircuitBreaker:
    def _cache(self, client, logger):
        return EgpfCache(BUCKET, client=client, logger=logger, clock=lambda: NOW)

    def _disabled_warnings(self, caplog):
        return [r for r in _warnings(caplog) if "EGPF cache disabled" in r.getMessage()]

    def test_three_consecutive_failures_disable_gcs(self, logger, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        client = FakeClient(fail={"download": RuntimeError("timeout"), "upload": RuntimeError("timeout"),
                                  "list": RuntimeError("timeout")})
        c = self._cache(client, logger)
        assert c.disabled is False
        assert c.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is False
        assert c.disabled is False
        assert c.list_cached_days(SPID, HOUSE) is None
        assert c.disabled is True
        assert len(client.calls) == 3
        disabled = self._disabled_warnings(caplog)
        assert len(disabled) == 1
        assert "3 consecutive GCS failures" in disabled[0].getMessage() and BUCKET in disabled[0].getMessage()

        # 以後は GCS を一切呼ばない
        before_blob, before_bucket = client.blob_calls, len(client.bucket_calls)
        warnings_before = len(_warnings(caplog))
        client.fail.clear()  # GCS が復旧しても、このインスタンスでは使わない
        assert c.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert c.load_envelope(SPID, HOUSE, DAY, STS, ETS) is None
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is False
        assert c.list_cached_days(SPID, HOUSE) is None
        c.compare_and_log(SPID, HOUSE, DAY, STS, ETS, _response(3))
        assert len(client.calls) == 3
        assert client.blob_calls == before_blob and len(client.bucket_calls) == before_bucket
        assert client.objects == {}
        assert len(_warnings(caplog)) == warnings_before  # 遮断の WARNING は 1回だけ
        assert any("cache_filled=missing" in m for m in _infos(caplog))

    def test_success_resets_counter(self, logger, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        client = FakeClient(fail={"download": RuntimeError("timeout")})
        c = self._cache(client, logger)
        assert c.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert c.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is True  # 成功でリセット
        assert c.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert c.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert c.disabled is False
        assert self._disabled_warnings(caplog) == []
        assert c.load(SPID, HOUSE, DAY, STS, ETS) is None  # リセット後 3回目で遮断
        assert c.disabled is True

    def test_not_found_is_not_failure_and_resets(self, logger, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        client = FakeClient()
        c = self._cache(client, logger)
        for _ in range(GCS_MAX_CONSECUTIVE_FAILURES + 2):
            assert c.load(SPID, HOUSE, DAY, STS, ETS) is None  # NotFound（キャッシュ無し）
        assert c.disabled is False
        client.fail["list"] = RuntimeError("timeout")
        assert c.list_cached_days(SPID, HOUSE) is None
        assert c.list_cached_days(SPID, HOUSE) is None
        del client.fail["list"]
        assert c.load(SPID, HOUSE, DAY, STS, ETS) is None  # NotFound でリセット
        client.fail["list"] = RuntimeError("timeout")
        assert c.list_cached_days(SPID, HOUSE) is None
        assert c.list_cached_days(SPID, HOUSE) is None
        assert c.disabled is False
        assert self._disabled_warnings(caplog) == []

    def test_broken_object_is_not_gcs_failure(self, logger, client):
        c = self._cache(client, logger)
        client.put_raw(object_key(SPID, HOUSE, DAY), b"broken")
        for _ in range(GCS_MAX_CONSECUTIVE_FAILURES + 1):
            assert c.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert c.disabled is False

    def test_skipped_saves_do_not_touch_counter(self, logger, caplog):
        # 未確定日・不正レスポンス・read-only は GCS を呼ばないので、障害にも成功にも数えない
        client = FakeClient(fail={"download": RuntimeError("timeout")})
        c = self._cache(client, logger)
        c.load(SPID, HOUSE, DAY, STS, ETS)
        c.load(SPID, HOUSE, DAY, STS, ETS)
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(0)) is False
        nxt = DAY + timedelta(days=2)
        assert c.save(SPID, HOUSE, nxt, *day_bounds(nxt), _response(3)) is False
        c.load(SPID, HOUSE, DAY, STS, ETS)
        assert c.disabled is True


# ---------------------------------------------------------------------------
# モジュール規約
# ---------------------------------------------------------------------------
def test_storage_not_imported_at_module_top():
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api", "egpf_cache.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    top_imports = [line for line in src.splitlines() if line.startswith(("import ", "from "))]
    assert not any("google" in line for line in top_imports)
    assert egpf_cache.__version__
