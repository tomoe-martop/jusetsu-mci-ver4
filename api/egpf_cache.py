# -*- coding: utf-8 -*-
"""EGPF estimated_data（1分値）の GCS 共有キャッシュ。

- オブジェクトキー: estimated_data/v1/{spid}/{houseid}/{YYYYMMDD}.json.gz（日付は JST の対象日）
- 内容: EGPF の生レスポンス（全家電・二値化前）を封筒 JSON で包んで gzip 圧縮
- 読み出し時に検証し、不正・GCS 障害は「キャッシュ無し」として扱う（例外を外に出さない）
- 対象日が終わっていない日（ets が現在時刻より後）は保存しない
- GCS 呼び出しは短い timeout・retry で行い、連続障害が続いたらそのインスタンスでは以後 GCS を呼ばない

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
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Set, Tuple

__version__ = "1.1.0"

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

# GCS 呼び出しの時間面のフォールバック（遅い・つながらない障害で 1日ごとに長く待たされないように）
GCS_TIMEOUT_SEC = 5                  # 1リクエストの timeout（秒）
GCS_RETRY_DEADLINE_SEC = 10          # retry 全体の期限（秒）
GCS_MAX_CONSECUTIVE_FAILURES = 3     # 連続障害がこの回数に達したら、そのインスタンスでは以後 GCS を呼ばない

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


def _timestamps_count(response_json: Any) -> Optional[int]:
    """data[0].timestamps の件数。形が崩れていれば None。"""
    d0 = _data0(response_json)
    if d0 is None:
        return None
    timestamps = d0.get("timestamps")
    if not isinstance(timestamps, list):
        return None
    return len(timestamps)


def is_valid_response(response_json: Any, sts: int, ets: int) -> bool:
    """data[0].timestamps がリストで、件数が 1 以上 (ets-sts)/60 + 1 以下なら True。

    0 件は不正（データが無い日を保存・ヒットさせない）。上限は ets ちょうどの分を含む場合に備えて
    +1 する（1日 = 1441 件まで有効）。
    """
    count = _timestamps_count(response_json)
    if count is None:
        return False
    try:
        max_count = (int(ets) - int(sts)) // 60 + 1
    except Exception:
        return False
    return 1 <= count <= max_count


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


def _build_gcs_retry() -> Any:
    """google.cloud.storage.retry.DEFAULT_RETRY の期限を GCS_RETRY_DEADLINE_SEC に絞った Retry を返す。

    google-api-core のバージョンにより with_timeout / with_deadline のどちらかを使う。
    import・生成に失敗したら None（retry 指定なしで timeout だけ渡す）。
    """
    try:
        retry_module = importlib.import_module("google.cloud.storage.retry")
        base = retry_module.DEFAULT_RETRY
        if hasattr(base, "with_timeout"):
            return base.with_timeout(GCS_RETRY_DEADLINE_SEC)
        return base.with_deadline(GCS_RETRY_DEADLINE_SEC)
    except Exception:
        return None


class EgpfCache:
    """GCS 上の EGPF 1分値キャッシュ。GCS の障害は警告ログを出して None/False を返す。

    GCS の障害（NotFound 以外の例外。Client 生成失敗を含む）が GCS_MAX_CONSECUTIVE_FAILURES 回
    続いたら、このインスタンスでは以後 GCS を呼ばない（disabled=True）。正常応答でカウンタはリセット。
    clock は現在時刻（UNIX 秒）を返す関数。未確定日の判定に使う（テストで固定するため注入可能）。
    """

    _RETRY_UNSET = object()

    def __init__(self, bucket_name: str, client: Any = None, writable: bool = True,
                 logger: Optional[logging.Logger] = None, clock: Optional[Callable[[], float]] = None):
        self.bucket_name = bucket_name
        self.writable = writable
        self._client = client
        self._client_failed = False
        self._logger = logger or _module_logger
        self._clock = clock or time.time
        self._retry = self._RETRY_UNSET
        self._consecutive_failures = 0
        self._disabled = False

    @property
    def disabled(self) -> bool:
        """連続障害で GCS の利用を止めていれば True（事前取得ジョブの打ち切り判定用）。"""
        return self._disabled

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

    def _gcs_kwargs(self) -> Dict[str, Any]:
        """GCS 呼び出しに渡す timeout・retry。retry は初回に 1度だけ組み立てる。"""
        if self._retry is self._RETRY_UNSET:
            self._retry = _build_gcs_retry()
        kwargs: Dict[str, Any] = {"timeout": GCS_TIMEOUT_SEC}
        if self._retry is not None:
            kwargs["retry"] = self._retry
        return kwargs

    def _gcs_ok(self) -> None:
        self._consecutive_failures = 0

    def _gcs_failed(self) -> None:
        if self._disabled:
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= GCS_MAX_CONSECUTIVE_FAILURES:
            self._disabled = True
            self._logger.warning("EGPF cache disabled after %d consecutive GCS failures "
                                 "(fallback to EGPF for the rest of this run): bucket=%s",
                                 self._consecutive_failures, self.bucket_name)

    def _warn(self, action: str, spid: Any, houseid: Any, day: Any, exc: BaseException) -> None:
        self._logger.warning("EGPF cache %s failed (fallback to EGPF): bucket=%s spid=%s house=%s date=%s %s: %s",
                             action, self.bucket_name, spid, houseid, day, type(exc).__name__, exc)

    # --- 読み出し -------------------------------------------------------------
    def load_envelope(self, spid: Any, houseid: Any, day: date, sts: int, ets: int) -> Optional[Dict[str, Any]]:
        """検証済みの封筒を返す。無い・不正・GCS 障害は None（障害・不正は警告ログ）。"""
        key = object_key(spid, houseid, day)
        if self._disabled:
            return None
        try:
            raw = self._bucket().blob(key).download_as_bytes(**self._gcs_kwargs())
        except Exception as e:
            if _is_not_found(e):
                self._gcs_ok()
                self._logger.debug("EGPF cache miss: %s", key)
            else:
                self._warn("read", spid, houseid, day, e)
                self._gcs_failed()
            return None
        self._gcs_ok()

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
            return "invalid response (timestamps=%s)" % (_timestamps_count(envelope.get("response")),)
        return None

    def load(self, spid: Any, houseid: Any, day: date, sts: int, ets: int) -> Optional[Any]:
        """キャッシュ済みの EGPF レスポンス JSON を返す。無ければ None。"""
        envelope = self.load_envelope(spid, houseid, day, sts, ets)
        if envelope is None:
            return None
        return envelope["response"]

    # --- 書き込み -------------------------------------------------------------
    def save(self, spid: Any, houseid: Any, day: date, sts: int, ets: int, response_json: Any) -> bool:
        """レスポンスを保存する。例外は出さず、次の場合は保存せず False を返す。

        - writable=False
        - 対象日が終わっていない（ets が現在時刻より後。当日・未来日）
        - 不正レスポンス（timestamps が 0 件・上限超えなど）
        - 連続障害で GCS の利用を止めている（disabled）・GCS 障害
        """
        if not self.writable:
            return False
        try:
            now = float(self._clock())
            unfinished = int(ets) > now
        except Exception as e:
            self._logger.warning("EGPF cache save skipped (time check failed): spid=%s house=%s date=%s %s: %s",
                                 spid, houseid, day, type(e).__name__, e)
            return False
        if unfinished:
            self._logger.info("EGPF cache save skipped (day not finished): spid=%s house=%s date=%s ets=%s now=%d",
                              spid, houseid, day, ets, int(now))
            return False
        if not is_valid_response(response_json, sts, ets):
            self._logger.warning("EGPF cache save skipped (invalid response): spid=%s house=%s date=%s timestamps=%s",
                                 spid, houseid, day, _timestamps_count(response_json))
            return False
        if self._disabled:
            return False
        key = object_key(spid, houseid, day)
        try:
            envelope = build_envelope(spid, houseid, day, sts, ets, response_json)
            body = gzip.compress(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except Exception as e:
            self._logger.warning("EGPF cache save skipped (encode failed): key=%s %s: %s", key, type(e).__name__, e)
            return False
        try:
            blob = self._bucket().blob(key)
            # Content-Encoding は付けない（GCS の自動解凍を避ける）
            blob.metadata = {
                "fetched_at": envelope["fetched_at"],
                "filled_minutes": str(envelope["filled_minutes"]),
            }
            blob.upload_from_string(body, content_type=CONTENT_TYPE, **self._gcs_kwargs())
        except Exception as e:
            self._warn("write", spid, houseid, day, e)
            self._gcs_failed()
            return False
        self._gcs_ok()
        self._logger.info("EGPF cache saved: key=%s timestamps=%d filled_minutes=%d",
                          key, _timestamps_count(response_json), envelope["filled_minutes"])
        return True

    # --- 一覧 -----------------------------------------------------------------
    def list_cached_days(self, spid: Any, houseid: Any) -> Optional[Set[date]]:
        """保存済みの日付（date）の集合を返す。GCS 障害・disabled は None（障害は警告ログ）。"""
        if self._disabled:
            return None
        prefix = _house_prefix(spid, houseid)
        days = set()
        try:
            for blob in self._get_client().list_blobs(self.bucket_name, prefix=prefix, **self._gcs_kwargs()):
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
            self._gcs_failed()
            return None
        self._gcs_ok()
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
    """EGPF_CACHE_BUCKET が設定されていれば EgpfCache を返す（GCS クライアントはまだ作らない）。未設定は None。"""
    bucket = get_bucket_name()
    if bucket is None:
        return None
    return EgpfCache(bucket, writable=writable, logger=logger)
