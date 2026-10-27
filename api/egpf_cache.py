# -*- coding: utf-8 -*-
"""EGPF estimated_data（1分値）の GCS 共有キャッシュ。

- オブジェクトキー: estimated_data/v1/{spid}/{houseid}/{YYYYMMDD}.json.gz（日付は JST の対象日）
- 内容: EGPF の生レスポンス（全家電・二値化前）を封筒 JSON で包んで gzip 圧縮
- 読み出し時に検証し、不正・GCS 障害は「キャッシュ無し」として扱う（例外を外に出さない）

設計書: docs/EG_DASHBOARD-102 【高負荷対応】MCI用の事前データ取得/設計書_EGPF1分値の事前取得・キャッシュ設計.md
（3.2・3.6・3.7）

【重要】このファイルは以下 2 リポジトリに同一内容でコピー配置する
  - jusetsu-mci-ver4/api/egpf_cache.py   （読み書き）
  - jusetsu-mci-ver3/api/egpf_cache.py   （読み取りのみ。Python 3.8）
そのため Python 3.8 互換で書き、依存は標準ライブラリと google-cloud-storage のみとする
（他ファイルを import しない。list[int] / X | Y / match 文 / walrus は使わない）。
google.cloud.storage は関数内で遅延 import する（未導入・import 失敗でも本モジュールは読み込める）。
変更時は __version__ を上げ、両リポジトリに反映する。
"""
import gzip
import importlib
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set, Tuple

__version__ = "1.0.1"

# ---------------------------------------------------------------------------
# 定数・環境変数（呼び出し時に読む。import 時に固定しない）
# ---------------------------------------------------------------------------
ENV_CACHE_ENABLED = "EGPF_CACHE_ENABLED"
ENV_CACHE_BUCKET = "EGPF_CACHE_BUCKET"

SCHEMA = "mci-egpf-estimated-data/v1"
KEY_PREFIX = "estimated_data/v1"
KEY_SUFFIX = ".json.gz"
CONTENT_TYPE = "application/gzip"

# 対象9家電（main.py の app_type_ids と同じ）
APP_TYPE_IDS = (2, 5, 20, 24, 25, 30, 31, 37, 301)

JST = timezone(timedelta(hours=9))

_TRUE_VALUES = ("1", "true", "yes", "on")

_module_logger = logging.getLogger(__name__)


def get_bucket_name() -> Optional[str]:
    """EGPF_CACHE_BUCKET を返す。未設定・空白のみは None。"""
    raw = (os.environ.get(ENV_CACHE_BUCKET) or "").strip()
    return raw or None


def is_cache_enabled() -> bool:
    """EGPF_CACHE_ENABLED が ON（1/true/yes/on）かつバケット設定ありなら True。"""
    raw = (os.environ.get(ENV_CACHE_ENABLED) or "").strip().lower()
    return raw in _TRUE_VALUES and get_bucket_name() is not None


def day_bounds(day: date) -> Tuple[int, int]:
    """対象日の JST 0:00 〜 翌日 JST 0:00 を UNIX 秒で返す。"""
    start = datetime(day.year, day.month, day.day, tzinfo=JST)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def _house_prefix(spid: Any, houseid: Any) -> str:
    return "%s/%s/%s/" % (KEY_PREFIX, spid, houseid)


def object_key(spid: Any, houseid: Any, day: date) -> str:
    """estimated_data/v1/{spid}/{houseid}/{YYYYMMDD}.json.gz"""
    return "%s%s%s" % (_house_prefix(spid, houseid), day.strftime("%Y%m%d"), KEY_SUFFIX)


def _data0(response_json: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(response_json, dict):
        return None
    data = response_json.get("data")
    if not isinstance(data, list) or len(data) == 0 or not isinstance(data[0], dict):
        return None
    return data[0]


def count_filled_minutes(response_json: Any) -> int:
    """対象9家電のいずれかが非 null の分数を数える。

    main.py の二値化ループで空行にならない分（exist=True）と同じ基準:
    各家電の appliances[0].powers[i] が None でなく数値に変換できれば埋まっているとみなす
    （0.0 も埋まっている扱い）。形が崩れている要素は数えない。例外は出さない。
    """
    d0 = _data0(response_json)
    if d0 is None:
        return 0
    timestamps = d0.get("timestamps")
    if not isinstance(timestamps, list):
        return 0
    appliance_types = d0.get("appliance_types")
    if not isinstance(appliance_types, list):
        appliance_types = []

    # 対象家電の powers 配列だけ取り出す
    powers_list = []
    for appliance_type in appliance_types:
        try:
            if int(appliance_type["appliance_type_id"]) not in APP_TYPE_IDS:
                continue
            powers = appliance_type["appliances"][0]["powers"]
        except Exception:
            continue
        if isinstance(powers, list):
            powers_list.append(powers)

    filled = 0
    for i in range(len(timestamps)):
        for powers in powers_list:
            if i >= len(powers) or powers[i] is None:
                continue
            try:
                float(powers[i])
            except Exception:
                continue
            filled += 1
            break
    return filled


def is_valid_response(response_json: Any, sts: int, ets: int) -> bool:
    """data[0].timestamps がリストで、件数が 0 以上 (ets-sts)/60 以下なら True。"""
    d0 = _data0(response_json)
    if d0 is None:
        return False
    timestamps = d0.get("timestamps")
    if not isinstance(timestamps, list):
        return False
    try:
        max_count = (int(ets) - int(sts)) // 60
    except Exception:
        return False
    return 0 <= len(timestamps) <= max_count


def _format_fetched_at(fetched_at: Any) -> str:
    if fetched_at is None:
        return datetime.now(JST).isoformat(timespec="seconds")
    if isinstance(fetched_at, datetime):
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=JST)
        return fetched_at.astimezone(JST).isoformat(timespec="seconds")
    return str(fetched_at)


def build_envelope(spid: Any, houseid: Any, day: date, sts: int, ets: int, response_json: Any,
                   fetched_at: Any = None) -> Dict[str, Any]:
    """保存用の封筒 JSON（dict）を組み立てる。"""
    return {
        "schema": SCHEMA,
        "spid": str(spid),
        "house": str(houseid),
        "date": day.strftime("%Y-%m-%d"),
        "sts": int(sts),
        "ets": int(ets),
        "fetched_at": _format_fetched_at(fetched_at),
        "filled_minutes": count_filled_minutes(response_json),
        "response": response_json,
    }


def _is_not_found(exc: BaseException) -> bool:
    # google.api_core.exceptions.NotFound を import せずに判定する
    return type(exc).__name__ == "NotFound" or getattr(exc, "code", None) == 404


class EgpfCache:
    """GCS 上の EGPF 1分値キャッシュ。GCS の障害は警告ログを出して None/False を返す。"""

    def __init__(self, bucket_name: str, client: Any = None, writable: bool = True,
                 logger: Optional[logging.Logger] = None):
        self.bucket_name = bucket_name
        self.writable = writable
        self._client = client
        self._client_failed = False
        self._logger = logger or _module_logger

    # --- GCS 接続 -----------------------------------------------------------
    def _get_client(self) -> Any:
        """初回利用時に google.cloud.storage.Client() を生成する。失敗は例外のまま返す。"""
        if self._client is not None:
            return self._client
        if self._client_failed:
            raise RuntimeError("GCS client is unavailable (creation failed earlier)")
        try:
            storage = importlib.import_module("google.cloud.storage")
            self._client = storage.Client()
        except Exception:
            # 生成失敗はこのインスタンスでは再試行しない（ハウス・日ごとに待たされないように）
            self._client_failed = True
            raise
        return self._client

    def _bucket(self) -> Any:
        return self._get_client().bucket(self.bucket_name)

    def _warn(self, action: str, spid: Any, houseid: Any, day: Any, exc: BaseException) -> None:
        self._logger.warning("EGPF cache %s failed (fallback to EGPF): bucket=%s spid=%s house=%s date=%s %s: %s",
                             action, self.bucket_name, spid, houseid, day, type(exc).__name__, exc)

    # --- 読み出し -------------------------------------------------------------
    def load_envelope(self, spid: Any, houseid: Any, day: date, sts: int, ets: int) -> Optional[Dict[str, Any]]:
        """検証済みの封筒を返す。無い・不正・GCS 障害は None（障害・不正は警告ログ）。"""
        key = object_key(spid, houseid, day)
        try:
            raw = self._bucket().blob(key).download_as_bytes()
        except Exception as e:
            if _is_not_found(e):
                self._logger.debug("EGPF cache miss: %s", key)
            else:
                self._warn("read", spid, houseid, day, e)
            return None

        try:
            envelope = json.loads(gzip.decompress(raw).decode("utf-8"))
        except Exception as e:
            self._logger.warning("EGPF cache broken (ignored): key=%s %s: %s", key, type(e).__name__, e)
            return None

        reason = self._invalid_reason(envelope, sts, ets)
        if reason is not None:
            self._logger.warning("EGPF cache invalid (ignored): key=%s reason=%s", key, reason)
            return None
        return envelope

    @staticmethod
    def _invalid_reason(envelope: Any, sts: int, ets: int) -> Optional[str]:
        if not isinstance(envelope, dict):
            return "not an object"
        if envelope.get("schema") != SCHEMA:
            return "schema=%r" % (envelope.get("schema"),)
        if envelope.get("sts") != int(sts) or envelope.get("ets") != int(ets):
            return "sts/ets=%r/%r expected=%s/%s" % (envelope.get("sts"), envelope.get("ets"), sts, ets)
        if not is_valid_response(envelope.get("response"), sts, ets):
            return "invalid response"
        return None

    def load(self, spid: Any, houseid: Any, day: date, sts: int, ets: int) -> Optional[Any]:
        """キャッシュ済みの EGPF レスポンス JSON を返す。無ければ None。"""
        envelope = self.load_envelope(spid, houseid, day, sts, ets)
        if envelope is None:
            return None
        return envelope["response"]

    # --- 書き込み -------------------------------------------------------------
    def save(self, spid: Any, houseid: Any, day: date, sts: int, ets: int, response_json: Any) -> bool:
        """レスポンスを保存する。writable=False・不正レスポンス・GCS 障害は False（例外は出さない）。"""
        if not self.writable:
            return False
        if not is_valid_response(response_json, sts, ets):
            self._logger.warning("EGPF cache save skipped (invalid response): spid=%s house=%s date=%s",
                                 spid, houseid, day)
            return False
        try:
            envelope = build_envelope(spid, houseid, day, sts, ets, response_json)
            body = gzip.compress(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            blob = self._bucket().blob(object_key(spid, houseid, day))
            # Content-Encoding は付けない（GCS の自動解凍を避ける）
            blob.metadata = {
                "fetched_at": envelope["fetched_at"],
                "filled_minutes": str(envelope["filled_minutes"]),
            }
            blob.upload_from_string(body, content_type=CONTENT_TYPE)
        except Exception as e:
            self._warn("write", spid, houseid, day, e)
            return False
        return True

    # --- 一覧 -----------------------------------------------------------------
    def list_cached_days(self, spid: Any, houseid: Any) -> Optional[Set[date]]:
        """保存済みの日付（date）の集合を返す。GCS 障害は None（警告ログ）。"""
        prefix = _house_prefix(spid, houseid)
        days = set()
        try:
            for blob in self._get_client().list_blobs(self.bucket_name, prefix=prefix):
                name = getattr(blob, "name", "") or ""
                rest = name[len(prefix):] if name.startswith(prefix) else ""
                if not rest.endswith(KEY_SUFFIX):
                    continue
                try:
                    days.add(datetime.strptime(rest[:-len(KEY_SUFFIX)], "%Y%m%d").date())
                except ValueError:
                    continue
        except Exception as e:
            self._warn("list", spid, houseid, "-", e)
            return None
        return days

    # --- 比較ログ（設計書 5.1） -------------------------------------------------
    def compare_and_log(self, spid: Any, houseid: Any, day: date, sts: int, ets: int,
                        egpf_response_json: Any) -> None:
        """キャッシュと EGPF 取得値の filled_minutes を比較して INFO ログに出す。例外は出さない。"""
        try:
            egpf_filled = count_filled_minutes(egpf_response_json)
            envelope = self.load_envelope(spid, houseid, day, sts, ets)
            if envelope is None:
                cache_filled = "missing"
                diff = "-"
            else:
                cache_filled = count_filled_minutes(envelope["response"])
                diff = egpf_filled - cache_filled
            self._logger.info("EGPF cache compare: spid=%s house=%s date=%s cache_filled=%s egpf_filled=%s diff=%s",
                              spid, houseid, day.strftime("%Y-%m-%d"), cache_filled, egpf_filled, diff)
        except Exception as e:
            self._logger.warning("EGPF cache compare failed: spid=%s house=%s date=%s %s: %s",
                                 spid, houseid, day, type(e).__name__, e)


def create_cache(writable: bool = True, logger: Optional[logging.Logger] = None) -> Optional[EgpfCache]:
    """EGPF_CACHE_ENABLED が ON かつバケット設定ありなら EgpfCache を返す。未設定/無効時は None。"""
    if not is_cache_enabled():
        return None
    bucket = get_bucket_name()
    if bucket is None:
        return None
    return EgpfCache(bucket, writable=writable, logger=logger)
