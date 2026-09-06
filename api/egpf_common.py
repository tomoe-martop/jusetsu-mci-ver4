# -*- coding: utf-8 -*-
"""EGPF（Energy Gateway Platform）連携の共通モジュール。

- リトライ付き GET（egpf_get）
- 連続失敗による打ち切り判定（ConsecutiveFailureGuard）
- Slack Incoming Webhook へのエラー通知（notify_error）
- 失敗分類・通知本文の組み立て（classify_failure / describe_failure / format_task_summary）

設計書: docs/EGPF連携リトライ・エラー通知対応/設計書_リトライ共通仕様・通知設計.md（3章・4章・5章）

【重要】このファイルは以下 3 リポジトリにコピー配置し、同一内容を保つこと
  - jusetsu-mci-ver4/api/egpf_common.py
  - jusetsu-mci-ver3/api/egpf_common.py   （Python 3.8）
  - jusetsu-csv-function/egpf_common.py
そのため Python 3.8 互換で書き、依存は requests と標準ライブラリのみとする
（他ファイルを import しない。list[int] / X | Y / match 文 / dataclass(slots=) は使わない）。
変更時は __version__ を上げ、3 リポジトリすべてに反映する。
"""
import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# 環境変数（既定値）。呼び出し時に読む（import 時に固定しない）
# ---------------------------------------------------------------------------
ENV_RETRY_MAX_ATTEMPTS = "EGPF_RETRY_MAX_ATTEMPTS"
ENV_RETRY_WAIT_SEC = "EGPF_RETRY_WAIT_SEC"
ENV_CONNECT_TIMEOUT_SEC = "EGPF_CONNECT_TIMEOUT_SEC"
ENV_READ_TIMEOUT_SEC = "EGPF_READ_TIMEOUT_SEC"
ENV_ABORT_AFTER_CONSECUTIVE_FAILURES = "EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES"
ENV_NOTIFY_WEBHOOK_URL = "ERROR_NOTIFY_SLACK_WEBHOOK_URL"
ENV_NOTIFY_ENV_LABEL = "ERROR_NOTIFY_ENV_LABEL"

DEFAULT_RETRY_MAX_ATTEMPTS = 5      # 初回 1 + 再送 4
DEFAULT_RETRY_WAIT_SEC = 2.0        # 固定待ち（バックオフ無し）
DEFAULT_CONNECT_TIMEOUT_SEC = 10.0
DEFAULT_READ_TIMEOUT_SEC = 30.0
DEFAULT_ABORT_AFTER_CONSECUTIVE_FAILURES = 3   # 0 で無効
NOTIFY_TIMEOUT_SEC = 10.0
NOTIFY_MAX_FAILURE_LINES = 20       # 失敗ハウスの列挙上限。超過分は「他 N 件」

# 失敗分類（設計書 4.5）
FAILURE_EGPF_COMM = "EGPF通信エラー"
FAILURE_EGPF_RESPONSE = "EGPF応答エラー"
FAILURE_NO_DATA = "データ無し"
FAILURE_TOTAL_LOSS = "Total loss"
FAILURE_DATA_SHORTAGE = "充足率不足"
FAILURE_PREDICTION = "予測処理エラー"
FAILURE_OTHER = "その他"

# 通知タイトル（MCI Ver3/Ver4 で共通に使う）
TITLE_TASK_FAILED = "月次予測 失敗あり"
TITLE_TASK_ABORTED = "EGPF 障害の疑い（タスク打ち切り）"
TITLE_UNEXPECTED = "予期しないエラー"
ACTION_RECREATE_TASK = "ダッシュボード「頭の安心チェック」から再実行タスクを作成してください"

# 「充足率不足」（Ver4: pred_mci status 202 / Ver3 相当）を示すメッセージ断片
_DATA_SHORTAGE_HINTS = (
    "必要な電力データ量を満たしていません",
    "ElectricDataShortage",
)
# 「予測処理エラー」を示す例外クラス名（api/myexception.py）とメッセージ断片（main.get_status_message）
_PREDICTION_EXCEPTION_NAMES = (
    "MyException", "InvalidInputError", "PredictionError", "PredictionTimeOut", "UnexpectedError",
)
_PREDICTION_ERROR_HINTS = (
    "CSVファイルが見つかりません",
    "電力データフォーマットエラー",
    "電力データが空です",
    "背景データフォーマットエラー",
    "電力モデルが見つかりません",
    "電力モデル読み込みエラー",
    "電力モデル予測時のエラー",
    "背景モデルが見つかりません",
    "背景モデル読み込みエラー",
    "背景モデル予測時のエラー",
    "予測時のタイムアウト",
    "予期せぬエラー",
)

_module_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------
class EgpfError(Exception):
    """EGPF 連携の共通例外。"""

    def __init__(self, message: str, context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.context = context or {}


class EgpfRetryExhausted(EgpfError):
    """通信系エラー（接続失敗・タイムアウト・本文受信中の切断・5xx・429）でリトライが枯渇した。"""

    def __init__(self, attempts: int, last_error: str, context: Optional[Dict[str, Any]] = None,
                 last_status: Optional[int] = None):
        super().__init__("EGPF request failed after %d attempt(s): %s" % (attempts, last_error), context)
        self.attempts = attempts
        self.last_error = last_error
        self.last_status = last_status


class EgpfClientError(EgpfError):
    """HTTP 4xx（429 を除く）。再送しても解消しないため即時失敗。"""

    def __init__(self, status: int, body: str = "", context: Optional[Dict[str, Any]] = None):
        super().__init__("EGPF returned HTTP %d" % status, context)
        self.status = status
        self.body = body


# ---------------------------------------------------------------------------
# 環境変数の読み込み
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        _module_logger.warning("%s=%r is not an integer. using default %s", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        _module_logger.warning("%s=%r is not a number. using default %s", name, raw, default)
        return default


def get_retry_config() -> Dict[str, Any]:
    """リトライ設定を環境変数から読む（テスト・ログ出力用に公開）。"""
    return {
        "max_attempts": max(1, _env_int(ENV_RETRY_MAX_ATTEMPTS, DEFAULT_RETRY_MAX_ATTEMPTS)),
        "wait_sec": max(0.0, _env_float(ENV_RETRY_WAIT_SEC, DEFAULT_RETRY_WAIT_SEC)),
        "connect_timeout_sec": _env_float(ENV_CONNECT_TIMEOUT_SEC, DEFAULT_CONNECT_TIMEOUT_SEC),
        "read_timeout_sec": _env_float(ENV_READ_TIMEOUT_SEC, DEFAULT_READ_TIMEOUT_SEC),
        "abort_after_consecutive_failures": max(
            0, _env_int(ENV_ABORT_AFTER_CONSECUTIVE_FAILURES, DEFAULT_ABORT_AFTER_CONSECUTIVE_FAILURES)),
    }


def _format_context(context: Optional[Dict[str, Any]]) -> str:
    if not context:
        return ""
    return " ".join("%s=%s" % (k, v) for k, v in context.items())


def _format_wait(wait_sec: float) -> str:
    if float(wait_sec).is_integer():
        return "%ds" % int(wait_sec)
    return "%.1fs" % wait_sec


# ---------------------------------------------------------------------------
# リトライ付き GET（設計書 3.1〜3.4）
# ---------------------------------------------------------------------------
def egpf_get(url: str, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None, *,
             context: Optional[Dict[str, Any]] = None,
             logger: Optional[logging.Logger] = None) -> requests.Response:
    """EGPF に GET し、通信系エラーをリトライする。

    - 2xx: Response をそのまま返す（202 も含む。JSON 解釈・データ検証は呼び出し側）
    - 429 / 5xx / 接続失敗 / タイムアウト / 本文受信中の切断・デコード失敗: 固定待ちで再送。
      枯渇したら EgpfRetryExhausted
    - その他の 4xx（および 2xx/429/5xx 以外）: 再送せず即 EgpfClientError
    - 試行ごとに 1 行 INFO ログ（成功時も出す。設計書 3.3）
    """
    log = logger or _module_logger
    cfg = get_retry_config()
    max_attempts = cfg["max_attempts"]
    wait_sec = cfg["wait_sec"]
    timeout = (cfg["connect_timeout_sec"], cfg["read_timeout_sec"])
    ctx = _format_context(context)

    last_status = None  # type: Optional[int]
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        status_label = ""
        try:
            res = requests.get(url, headers=headers, params=params, timeout=timeout)
        except requests.exceptions.Timeout as e:
            # ConnectTimeout は Timeout と ConnectionError の両方を継承するので先に判定する
            if isinstance(e, requests.exceptions.ConnectTimeout):
                last_error = "connect timeout"
            elif isinstance(e, requests.exceptions.ReadTimeout):
                last_error = "read timeout"
            else:
                last_error = "timeout"
            last_status = None
            status_label = "timeout"
        except requests.exceptions.ConnectionError as e:
            last_error = "connection error: %s" % _one_line(str(e), 120)
            last_status = None
            status_label = "conn_error"
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError) as e:
            # 本文受信中の切断（ChunkedEncodingError）・本文デコード失敗（ContentDecodingError）は
            # RequestException 直下で ConnectionError 派生ではないため個別に捕捉し、接続失敗と同列に再送する
            detail = _one_line(str(e), 120)
            last_error = type(e).__name__ + (": " + detail if detail else "")
            last_status = None
            status_label = "conn_error"
        else:
            elapsed = time.monotonic() - started
            status = res.status_code
            if 200 <= status < 300:
                log.info("EGPF attempt=%d/%d status=%d elapsed=%.2fs %s ok",
                         attempt, max_attempts, status, elapsed, ctx)
                return res
            if status == 429 or 500 <= status < 600:
                last_status = status
                last_error = "HTTP %d" % status
                status_label = str(status)
            else:
                log.warning("EGPF attempt=%d/%d status=%d elapsed=%.2fs %s non-2xx, giving up",
                            attempt, max_attempts, status, elapsed, ctx)
                raise EgpfClientError(status, _safe_text(res), context)

        elapsed = time.monotonic() - started
        if attempt < max_attempts:
            log.info("EGPF attempt=%d/%d status=%s elapsed=%.2fs %s retry_in=%s",
                     attempt, max_attempts, status_label, elapsed, ctx, _format_wait(wait_sec))
            time.sleep(wait_sec)
        else:
            log.warning("EGPF attempt=%d/%d status=%s elapsed=%.2fs %s giving up",
                        attempt, max_attempts, status_label, elapsed, ctx)

    raise EgpfRetryExhausted(max_attempts, last_error, context, last_status=last_status)


def _safe_text(res: requests.Response, limit: int = 500) -> str:
    try:
        return (res.text or "")[:limit]
    except Exception:
        return ""


def _one_line(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) > limit:
        return text[:limit - 1] + "…"
    return text


# ---------------------------------------------------------------------------
# 連続失敗による打ち切り（設計書 3.5）
# ---------------------------------------------------------------------------
class ConsecutiveFailureGuard:
    """連続してリトライ枯渇したリクエスト数が閾値に達したら打ち切りを指示する。

    threshold=None なら EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES（既定 3）を読む。0 で無効。
    """

    def __init__(self, threshold: Optional[int] = None):
        if threshold is None:
            threshold = get_retry_config()["abort_after_consecutive_failures"]
        self.threshold = max(0, int(threshold))
        self.consecutive_failures = 0

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_exhausted(self) -> bool:
        """リトライ枯渇を 1 件記録し、打ち切るべきなら True を返す。"""
        self.consecutive_failures += 1
        return self.threshold > 0 and self.consecutive_failures >= self.threshold


# ---------------------------------------------------------------------------
# 失敗分類（設計書 4.5）
# ---------------------------------------------------------------------------
def classify_failure(exc: BaseException) -> str:
    """例外から失敗分類名を返す。ログ・通知にのみ使う（DB には書かない）。"""
    if isinstance(exc, EgpfRetryExhausted):
        return FAILURE_EGPF_COMM
    if isinstance(exc, EgpfClientError):
        return FAILURE_EGPF_RESPONSE
    # data[] が空: response_data['data'][0] の IndexError / 'data' キー無しの KeyError / JSON でない本文
    if isinstance(exc, (IndexError, KeyError)) or type(exc).__name__ == "JSONDecodeError":
        return FAILURE_NO_DATA

    message = str(exc)
    if isinstance(exc, ValueError) and "Total loss" in message:
        return FAILURE_TOTAL_LOSS

    status_code = getattr(exc, "status_code", None)
    if status_code == 202 or any(h in message for h in _DATA_SHORTAGE_HINTS):
        return FAILURE_DATA_SHORTAGE
    if (status_code is not None
            or type(exc).__name__ in _PREDICTION_EXCEPTION_NAMES
            or any(h in message for h in _PREDICTION_ERROR_HINTS)):
        return FAILURE_PREDICTION
    return FAILURE_OTHER


def describe_failure(exc: BaseException, limit: int = 200) -> str:
    """通知の失敗一覧に載せる 1 行の詳細を返す。"""
    if isinstance(exc, EgpfRetryExhausted):
        return "%d回送信して失敗: %s" % (exc.attempts, exc.last_error)
    if isinstance(exc, EgpfClientError):
        return "HTTP %d" % exc.status
    message = _one_line(str(exc), limit)
    if not message:
        return type(exc).__name__
    return "%s: %s" % (type(exc).__name__, message)


# ---------------------------------------------------------------------------
# 通知本文（設計書 4.4）
# ---------------------------------------------------------------------------
def _format_period(started_at: Any, ended_at: Any) -> str:
    if started_at is None and ended_at is None:
        return "-"
    if started_at is None:
        return "〜%s" % ended_at.strftime("%Y-%m-%d %H:%M")
    start_text = started_at.strftime("%Y-%m-%d %H:%M")
    if ended_at is None:
        return "%s〜" % start_text
    if ended_at.date() == started_at.date():
        return "%s〜%s" % (start_text, ended_at.strftime("%H:%M"))
    return "%s〜%s" % (start_text, ended_at.strftime("%Y-%m-%d %H:%M"))


def format_task_summary(task_id: Any, started_at: Any, ended_at: Any, total: int, succeeded: int,
                        failures: Sequence[Tuple[Any, str, str]], *,
                        remaining: Optional[int] = None,
                        log_path: Optional[str] = None,
                        action: Optional[str] = None) -> List[str]:
    """MCI タスク単位の通知本文（ヘッダ行を除く）を組み立てる純粋関数。

    failures: (houseid, 分類, 詳細) の列。列挙は NOTIFY_MAX_FAILURE_LINES 行まで、超過分は「他 N 件」。
    remaining: 打ち切り時の未処理ハウス数（None なら打ち切り無し）。
    """
    counts = "対象 %d件 / 成功 %d / 失敗 %d" % (total, succeeded, len(failures))
    if remaining is not None:
        counts += " / 未処理 %d" % remaining
    lines = [
        "task_id: %s　実行: %s" % (task_id, _format_period(started_at, ended_at)),
        counts,
    ]
    if remaining is not None:
        lines.append("EGPF への連続失敗によりタスクを打ち切りました（残り %d件は未処理。復旧後に再実行が必要）" % remaining)
    if failures:
        lines.append("失敗ハウス:")
        for houseid, category, detail in list(failures)[:NOTIFY_MAX_FAILURE_LINES]:
            if detail:
                lines.append(" - %s: %s（%s）" % (houseid, category, detail))
            else:
                lines.append(" - %s: %s" % (houseid, category))
        overflow = len(failures) - NOTIFY_MAX_FAILURE_LINES
        if overflow > 0:
            lines.append(" 他 %d 件" % overflow)
    if log_path:
        lines.append("ログ: %s" % log_path)
    if action:
        lines.append("対応: %s" % action)
    return lines


def build_notify_text(title: str, lines: Iterable[str], *, env_label: Optional[str] = None,
                      source: Optional[str] = None) -> str:
    """通知テキストを組み立てる。先頭行は `[source][env_label] title`（無い要素は省く）。"""
    header = ""
    if source:
        header += "[%s]" % source
    if env_label:
        header += "[%s]" % env_label
    header = (header + " " + title).strip() if header else title
    body = [str(line) for line in lines if line is not None]
    return "\n".join([header] + body)


def notify_error(title: str, lines: Iterable[str], *, env_label: Optional[str] = None,
                 logger: Optional[logging.Logger] = None, source: Optional[str] = None) -> bool:
    """Slack Incoming Webhook へ通知する。

    - ERROR_NOTIFY_SLACK_WEBHOOK_URL 未設定なら False を返して何もしない
    - env_label が None なら ERROR_NOTIFY_ENV_LABEL を使う
    - 送信失敗・例外は warning ログのみで False（通知失敗で本処理を止めない）。タイムアウト 10 秒
    - payload は {"text": ...} のみ（Block Kit は使わない）
    """
    log = logger or _module_logger
    webhook_url = (os.environ.get(ENV_NOTIFY_WEBHOOK_URL) or "").strip()
    if not webhook_url:
        log.debug("notify_error: %s is not set. skip notification: %s", ENV_NOTIFY_WEBHOOK_URL, title)
        return False
    if env_label is None:
        env_label = os.environ.get(ENV_NOTIFY_ENV_LABEL) or None

    text = build_notify_text(title, lines, env_label=env_label, source=source)
    try:
        res = requests.post(webhook_url, json={"text": text}, timeout=NOTIFY_TIMEOUT_SEC)
    except Exception as e:  # 通知失敗で本処理を止めない
        log.warning("notify_error: failed to send notification (%s): %s", title, e)
        return False
    if 200 <= res.status_code < 300:
        log.info("notify_error: sent notification: %s", title)
        return True
    log.warning("notify_error: webhook returned HTTP %s (%s): %s",
                res.status_code, title, _one_line(_safe_text(res, 200), 200))
    return False
