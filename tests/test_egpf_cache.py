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

    def download_as_bytes(self):
        if "download" in self._fail:
            raise self._fail["download"]
        if self.name not in self._store.objects:
            raise NotFound("No such object: %s" % self.name)
        return self._store.objects[self.name]["body"]

    def upload_from_string(self, data, content_type=None):
        self._store.upload_calls += 1
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

    def bucket(self, name):
        self.bucket_calls.append(name)
        return FakeBucket(self, self.fail)

    def list_blobs(self, bucket_name, prefix=None):
        self.list_calls += 1
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
    return EgpfCache(BUCKET, client=client, logger=logger)


@pytest.fixture
def env_clean(monkeypatch):
    monkeypatch.delenv(ENV_CACHE_BUCKET, raising=False)
    monkeypatch.delenv(ENV_CACHE_ENABLED, raising=False)
    return monkeypatch


def _warnings(caplog):
    return [r for r in caplog.records if r.name == LOGGER_NAME and r.levelno == logging.WARNING]


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
        assert is_valid_response(_response(0), STS, ETS) is True
        assert is_valid_response(_response(3), STS, ETS) is True
        assert is_valid_response({"data": [{"timestamps": list(range(1440))}]}, STS, ETS) is True

    def test_too_many_timestamps(self):
        assert is_valid_response({"data": [{"timestamps": list(range(1441))}]}, STS, ETS) is False

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

    @pytest.mark.parametrize("res", [{"data": []}, {}, None, {"data": [{"timestamps": list(range(1441))}]}])
    def test_invalid_response_not_saved(self, cache, client, caplog, res):
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        assert cache.save(SPID, HOUSE, DAY, STS, ETS, res) is False
        assert client.objects == {} and client.upload_calls == 0
        assert len(_warnings(caplog)) == 1

    def test_read_only_never_touches_gcs_on_save(self, client, logger):
        ro = EgpfCache(BUCKET, client=client, writable=False, logger=logger)
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
        env["response"] = {"data": [{"timestamps": list(range(1441)), "appliance_types": []}]}
        client.put_envelope(self.KEY, env)
        self._assert_discarded(cache, caplog)

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
        return EgpfCache(BUCKET, client=FakeClient(fail=fail), logger=logger)

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
        c = EgpfCache(BUCKET, logger=logger)
        assert calls == []  # 生成は初回利用時
        assert c.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is False
        assert c.list_cached_days(SPID, HOUSE) is None
        c.compare_and_log(SPID, HOUSE, DAY, STS, ETS, _response(3))
        assert len(_warnings(caplog)) == 4
        assert calls == [1]  # 失敗後は再生成しない

    def test_import_error(self, monkeypatch, logger, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        monkeypatch.setitem(sys.modules, "google.cloud.storage", None)  # import が ImportError になる
        c = EgpfCache(BUCKET, logger=logger)
        assert c.load(SPID, HOUSE, DAY, STS, ETS) is None
        assert c.save(SPID, HOUSE, DAY, STS, ETS, _response(3)) is False
        assert c.list_cached_days(SPID, HOUSE) is None
        assert len(_warnings(caplog)) == 3
        assert "ImportError" in _warnings(caplog)[0].getMessage() or \
            "ModuleNotFoundError" in _warnings(caplog)[0].getMessage()

    def test_lazy_client_from_storage_module(self, monkeypatch, logger):
        fake = FakeClient()
        calls = []
        fake_storage = types.ModuleType("google.cloud.storage")
        fake_storage.Client = lambda *a, **k: calls.append(1) or fake
        monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)
        c = EgpfCache(BUCKET, logger=logger)
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
        c = EgpfCache(BUCKET, client=FakeClient(fail={"download": RuntimeError("down")}), logger=logger)
        c.compare_and_log(SPID, HOUSE, DAY, STS, ETS, _response(3))
        assert "cache_filled=missing" in self._info(caplog)[0]

    def test_never_raises_and_does_not_write(self, cache, client):
        cache.compare_and_log(SPID, HOUSE, DAY, STS, ETS, None)
        cache.compare_and_log(SPID, HOUSE, "not a date", STS, ETS, _response(3))
        assert client.upload_calls == 0


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
