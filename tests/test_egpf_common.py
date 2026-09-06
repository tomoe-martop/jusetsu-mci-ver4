# -*- coding: utf-8 -*-
"""api/egpf_common.py の単体テスト（設計書 7.1 ほか）。

requests.get / requests.post / time.sleep は unittest.mock で差し替え、ネットワークは使わない。
実行: python -m pytest tests -q
"""
import logging
from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest
import requests

import egpf_common
from egpf_common import (
    ConsecutiveFailureGuard,
    EgpfClientError,
    EgpfError,
    EgpfRetryExhausted,
    FAILURE_DATA_SHORTAGE,
    FAILURE_EGPF_COMM,
    FAILURE_EGPF_RESPONSE,
    FAILURE_NO_DATA,
    FAILURE_OTHER,
    FAILURE_PREDICTION,
    FAILURE_TOTAL_LOSS,
    build_notify_text,
    classify_failure,
    describe_failure,
    egpf_get,
    format_task_summary,
    get_retry_config,
    notify_error,
)

URL = "https://api.example.test/0.2/estimated_data"
HEADERS = {"Authorization": "imSP 0187:secret"}
PARAMS = {"service_provider": "0187", "house": "MAEBASHI02", "sts": 1, "ets": 2, "time_units": 20}
CONTEXT = {"spid": "0187", "house": "MAEBASHI02", "sts": 1, "ets": 2}
ENV_NAMES = (
    "EGPF_RETRY_MAX_ATTEMPTS",
    "EGPF_RETRY_WAIT_SEC",
    "EGPF_CONNECT_TIMEOUT_SEC",
    "EGPF_READ_TIMEOUT_SEC",
    "EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES",
    "ERROR_NOTIFY_SLACK_WEBHOOK_URL",
    "ERROR_NOTIFY_ENV_LABEL",
)


def _resp(status, text="", json_body=None):
    res = MagicMock(spec=requests.Response)
    res.status_code = status
    res.text = text
    res.json.return_value = json_body if json_body is not None else {}
    return res


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """テスト間で環境変数が漏れないように、対象の環境変数を毎回未設定にする。"""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def mocked_io():
    """requests.get / time.sleep を差し替えて (get, sleep) を返す。"""
    with patch.object(egpf_common.requests, "get") as get, patch.object(egpf_common.time, "sleep") as sleep:
        yield get, sleep


@pytest.fixture
def test_logger(caplog):
    caplog.set_level(logging.DEBUG)
    return logging.getLogger("test_egpf_common")


def _attempt_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("EGPF attempt=")]


# ---------------------------------------------------------------------------
# egpf_get: 設計書 7.1 のケース
# ---------------------------------------------------------------------------
class TestEgpfGetRetry:
    def test_first_200_returns_immediately(self, mocked_io, caplog, test_logger):
        get, sleep = mocked_io
        ok = _resp(200, json_body={"data": [{"timestamps": []}]})
        get.return_value = ok

        res = egpf_get(URL, HEADERS, PARAMS, context=CONTEXT, logger=test_logger)

        assert res is ok
        assert get.call_count == 1
        sleep.assert_not_called()
        lines = _attempt_lines(caplog)
        assert len(lines) == 1
        assert lines[0].startswith("EGPF attempt=1/5 status=200 ")
        assert "spid=0187 house=MAEBASHI02 sts=1 ets=2" in lines[0]
        assert lines[0].endswith(" ok")

    def test_passes_headers_params_and_default_timeout(self, mocked_io):
        get, _ = mocked_io
        get.return_value = _resp(200)

        egpf_get(URL, HEADERS, PARAMS)

        get.assert_called_once_with(URL, headers=HEADERS, params=PARAMS, timeout=(10.0, 30.0))

    def test_503_503_200_succeeds_on_third_attempt(self, mocked_io, caplog, test_logger):
        get, sleep = mocked_io
        ok = _resp(200)
        get.side_effect = [_resp(503), _resp(503), ok]

        res = egpf_get(URL, HEADERS, PARAMS, context=CONTEXT, logger=test_logger)

        assert res is ok
        assert get.call_count == 3
        assert sleep.call_args_list == [call(2.0), call(2.0)]
        lines = _attempt_lines(caplog)
        assert len(lines) == 3
        assert lines[0].startswith("EGPF attempt=1/5 status=503 ")
        assert lines[0].endswith("retry_in=2s")
        assert lines[1].startswith("EGPF attempt=2/5 status=503 ")
        assert lines[2].startswith("EGPF attempt=3/5 status=200 ")
        # 試行ログは INFO で出る
        assert all(r.levelno == logging.INFO for r in caplog.records if r.getMessage().startswith("EGPF attempt="))

    def test_503_x5_raises_retry_exhausted(self, mocked_io, caplog, test_logger):
        get, sleep = mocked_io
        get.side_effect = [_resp(503, text="Service Unavailable")] * 5

        with pytest.raises(EgpfRetryExhausted) as ei:
            egpf_get(URL, HEADERS, PARAMS, context=CONTEXT, logger=test_logger)

        exc = ei.value
        assert isinstance(exc, EgpfError)
        assert exc.attempts == 5
        assert exc.last_status == 503
        assert exc.last_error == "HTTP 503"
        assert exc.context == CONTEXT
        assert get.call_count == 5
        assert sleep.call_args_list == [call(2.0)] * 4
        lines = _attempt_lines(caplog)
        assert len(lines) == 5
        assert lines[-1].startswith("EGPF attempt=5/5 status=503 ")
        assert lines[-1].endswith("giving up")

    def test_read_timeout_x5_raises_retry_exhausted(self, mocked_io, caplog, test_logger):
        get, sleep = mocked_io
        get.side_effect = requests.exceptions.ReadTimeout("read timed out")

        with pytest.raises(EgpfRetryExhausted) as ei:
            egpf_get(URL, HEADERS, PARAMS, context=CONTEXT, logger=test_logger)

        exc = ei.value
        assert exc.attempts == 5
        assert exc.last_status is None
        assert "timeout" in exc.last_error
        assert get.call_count == 5
        assert sleep.call_args_list == [call(2.0)] * 4
        lines = _attempt_lines(caplog)
        assert len(lines) == 5
        assert all("status=timeout" in line for line in lines)

    def test_connect_timeout_then_success(self, mocked_io):
        get, sleep = mocked_io
        ok = _resp(200)
        get.side_effect = [requests.exceptions.ConnectTimeout("connect timed out"), ok]

        assert egpf_get(URL, HEADERS, PARAMS) is ok
        assert get.call_count == 2
        assert sleep.call_args_list == [call(2.0)]

    def test_connection_error_is_retried(self, mocked_io, caplog, test_logger):
        get, sleep = mocked_io
        get.side_effect = requests.exceptions.ConnectionError("Name or service not known")

        with pytest.raises(EgpfRetryExhausted) as ei:
            egpf_get(URL, HEADERS, PARAMS, logger=test_logger)

        assert ei.value.attempts == 5
        assert ei.value.last_error.startswith("connection error")
        assert sleep.call_count == 4
        assert all("status=conn_error" in line for line in _attempt_lines(caplog))

    def test_429_then_200_succeeds_on_second_attempt(self, mocked_io):
        get, sleep = mocked_io
        ok = _resp(200)
        get.side_effect = [_resp(429), ok]

        assert egpf_get(URL, HEADERS, PARAMS) is ok
        assert get.call_count == 2
        assert sleep.call_args_list == [call(2.0)]

    def test_404_raises_client_error_without_retry(self, mocked_io, caplog, test_logger):
        get, sleep = mocked_io
        get.return_value = _resp(404, text='{"error":"not found"}')

        with pytest.raises(EgpfClientError) as ei:
            egpf_get(URL, HEADERS, PARAMS, context=CONTEXT, logger=test_logger)

        exc = ei.value
        assert isinstance(exc, EgpfError)
        assert exc.status == 404
        assert exc.body == '{"error":"not found"}'
        assert exc.context == CONTEXT
        assert get.call_count == 1
        sleep.assert_not_called()
        lines = _attempt_lines(caplog)
        assert len(lines) == 1
        assert "status=404" in lines[0] and lines[0].endswith("client error, giving up")

    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_other_4xx_raise_client_error(self, mocked_io, status):
        get, sleep = mocked_io
        get.return_value = _resp(status)

        with pytest.raises(EgpfClientError) as ei:
            egpf_get(URL, HEADERS, PARAMS)

        assert ei.value.status == status
        assert get.call_count == 1
        sleep.assert_not_called()

    def test_202_is_returned_as_is(self, mocked_io):
        get, sleep = mocked_io
        accepted = _resp(202)
        get.return_value = accepted

        assert egpf_get(URL, HEADERS, PARAMS) is accepted
        sleep.assert_not_called()

    def test_max_attempts_from_env(self, mocked_io, monkeypatch):
        monkeypatch.setenv("EGPF_RETRY_MAX_ATTEMPTS", "3")
        get, sleep = mocked_io
        get.return_value = _resp(503)

        with pytest.raises(EgpfRetryExhausted) as ei:
            egpf_get(URL, HEADERS, PARAMS)

        assert ei.value.attempts == 3
        assert get.call_count == 3
        assert sleep.call_args_list == [call(2.0)] * 2

    def test_wait_and_timeouts_from_env(self, mocked_io, monkeypatch):
        monkeypatch.setenv("EGPF_RETRY_WAIT_SEC", "5")
        monkeypatch.setenv("EGPF_CONNECT_TIMEOUT_SEC", "3")
        monkeypatch.setenv("EGPF_READ_TIMEOUT_SEC", "7")
        get, sleep = mocked_io
        get.side_effect = [_resp(500), _resp(200)]

        egpf_get(URL, HEADERS, PARAMS)

        assert sleep.call_args_list == [call(5.0)]
        assert get.call_args.kwargs["timeout"] == (3.0, 7.0)

    def test_env_is_read_at_call_time_not_import_time(self, mocked_io, monkeypatch):
        get, _ = mocked_io
        get.return_value = _resp(200)
        egpf_get(URL, HEADERS, PARAMS)
        assert get.call_args.kwargs["timeout"] == (10.0, 30.0)

        monkeypatch.setenv("EGPF_READ_TIMEOUT_SEC", "45")
        egpf_get(URL, HEADERS, PARAMS)
        assert get.call_args.kwargs["timeout"] == (10.0, 45.0)

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("EGPF_RETRY_MAX_ATTEMPTS", "abc")
        monkeypatch.setenv("EGPF_RETRY_WAIT_SEC", "")
        cfg = get_retry_config()
        assert cfg["max_attempts"] == 5
        assert cfg["wait_sec"] == 2.0

    def test_non_retryable_request_exception_propagates(self, mocked_io):
        get, sleep = mocked_io
        get.side_effect = requests.exceptions.InvalidURL("bad url")

        with pytest.raises(requests.exceptions.InvalidURL):
            egpf_get(URL, HEADERS, PARAMS)
        sleep.assert_not_called()


# ---------------------------------------------------------------------------
# ConsecutiveFailureGuard（設計書 3.5）
# ---------------------------------------------------------------------------
class TestConsecutiveFailureGuard:
    def test_default_threshold_is_3(self):
        guard = ConsecutiveFailureGuard()
        assert guard.threshold == 3
        assert guard.record_exhausted() is False
        assert guard.record_exhausted() is False
        assert guard.record_exhausted() is True

    def test_success_resets_counter(self):
        guard = ConsecutiveFailureGuard(threshold=3)
        assert guard.record_exhausted() is False
        assert guard.record_exhausted() is False
        guard.record_success()
        assert guard.record_exhausted() is False
        assert guard.record_exhausted() is False
        assert guard.record_exhausted() is True

    def test_threshold_zero_disables(self):
        guard = ConsecutiveFailureGuard(threshold=0)
        assert all(guard.record_exhausted() is False for _ in range(10))

    def test_threshold_from_env(self, monkeypatch):
        monkeypatch.setenv("EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES", "2")
        guard = ConsecutiveFailureGuard()
        assert guard.threshold == 2
        assert guard.record_exhausted() is False
        assert guard.record_exhausted() is True

        monkeypatch.setenv("EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES", "0")
        assert ConsecutiveFailureGuard().threshold == 0


# ---------------------------------------------------------------------------
# classify_failure / describe_failure（設計書 4.5）
# ---------------------------------------------------------------------------
class _FakeMyException(Exception):
    """api/myexception.py の MyException 相当（status_code 属性を持つ）。"""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class InvalidInputError(_FakeMyException):
    pass


class PredictionError(_FakeMyException):
    pass


class TestClassifyFailure:
    @pytest.mark.parametrize("exc, expected", [
        (EgpfRetryExhausted(5, "read timeout"), FAILURE_EGPF_COMM),
        (EgpfClientError(404, "not found"), FAILURE_EGPF_RESPONSE),
        (IndexError("list index out of range"), FAILURE_NO_DATA),
        (KeyError("data"), FAILURE_NO_DATA),
        (requests.exceptions.JSONDecodeError("Expecting value", "", 0), FAILURE_NO_DATA),
        (ValueError("Total loss error!"), FAILURE_TOTAL_LOSS),
        (Exception("必要な電力データ量を満たしていません"), FAILURE_DATA_SHORTAGE),
        (InvalidInputError(202, "Electric data shortage"), FAILURE_DATA_SHORTAGE),
        (Exception("電力モデル予測時のエラー"), FAILURE_PREDICTION),
        (Exception("予測時のタイムアウト"), FAILURE_PREDICTION),
        (Exception("予期せぬエラー"), FAILURE_PREDICTION),
        (PredictionError(302, "LightGBM prediction failed"), FAILURE_PREDICTION),
        (ValueError("something else"), FAILURE_OTHER),
        (RuntimeError("db gone"), FAILURE_OTHER),
    ])
    def test_classification(self, exc, expected):
        assert classify_failure(exc) == expected

    def test_describe_failure(self):
        assert describe_failure(EgpfRetryExhausted(5, "read timeout")) == "5回送信して失敗: read timeout"
        assert describe_failure(EgpfRetryExhausted(5, "HTTP 503", last_status=503)) == "5回送信して失敗: HTTP 503"
        assert describe_failure(EgpfClientError(404)) == "HTTP 404"
        assert describe_failure(ValueError("Total loss error!")) == "ValueError: Total loss error!"
        assert describe_failure(RuntimeError("line1\nline2")) == "RuntimeError: line1 line2"
        assert describe_failure(RuntimeError("")) == "RuntimeError"
        assert len(describe_failure(RuntimeError("x" * 500))) <= len("RuntimeError: ") + 200


# ---------------------------------------------------------------------------
# format_task_summary / build_notify_text（設計書 4.4）
# ---------------------------------------------------------------------------
class TestFormatTaskSummary:
    def test_matches_design_example(self):
        lines = format_task_summary(
            433, datetime(2026, 8, 2, 0, 0), datetime(2026, 8, 2, 0, 47), 12, 10,
            [("DUSKIN0001", FAILURE_EGPF_COMM, "5回送信して失敗: read timeout"),
             ("DUSKIN0003", FAILURE_EGPF_COMM, "5回送信して失敗: HTTP 503")],
            log_path="gs://prd-mci-ver4/logs/predictor_0000000433_20260802004700.log",
            action="ダッシュボード「頭の安心チェック」から再実行タスクを作成してください",
        )
        assert lines == [
            "task_id: 433　実行: 2026-08-02 00:00〜00:47",
            "対象 12件 / 成功 10 / 失敗 2",
            "失敗ハウス:",
            " - DUSKIN0001: EGPF通信エラー（5回送信して失敗: read timeout）",
            " - DUSKIN0003: EGPF通信エラー（5回送信して失敗: HTTP 503）",
            "ログ: gs://prd-mci-ver4/logs/predictor_0000000433_20260802004700.log",
            "対応: ダッシュボード「頭の安心チェック」から再実行タスクを作成してください",
        ]

    def test_caps_failure_lines_at_20(self):
        failures = [("H%03d" % i, FAILURE_TOTAL_LOSS, "ValueError: Total loss error!") for i in range(25)]
        lines = format_task_summary(1, None, None, 30, 5, failures)
        listed = [line for line in lines if line.startswith(" - ")]
        assert len(listed) == 20
        assert listed[0].startswith(" - H000:")
        assert listed[-1].startswith(" - H019:")
        assert " 他 5 件" in lines
        assert "task_id: 1　実行: -" == lines[0]
        assert "対象 30件 / 成功 5 / 失敗 25" == lines[1]

    def test_exactly_20_failures_has_no_overflow_line(self):
        failures = [("H%03d" % i, FAILURE_OTHER, "") for i in range(20)]
        lines = format_task_summary(1, None, None, 20, 0, failures)
        assert not any("他" in line and "件" in line for line in lines)
        assert " - H000: その他" in lines

    def test_aborted_task_includes_remaining(self):
        lines = format_task_summary(
            500, datetime(2026, 10, 2, 0, 0), datetime(2026, 10, 3, 0, 10), 40, 1,
            [("A", FAILURE_EGPF_COMM, "5回送信して失敗: HTTP 503")] * 3, remaining=36)
        assert lines[0] == "task_id: 500　実行: 2026-10-02 00:00〜2026-10-03 00:10"
        assert lines[1] == "対象 40件 / 成功 1 / 失敗 3 / 未処理 36"
        assert any("打ち切り" in line and "36件" in line for line in lines)
        assert not any(line.startswith("ログ:") for line in lines)

    def test_no_failures_no_log(self):
        lines = format_task_summary(7, None, None, 3, 3, [])
        assert lines == ["task_id: 7　実行: -", "対象 3件 / 成功 3 / 失敗 0"]

    def test_build_notify_text_header(self):
        text = build_notify_text("月次予測 失敗あり", ["a", "b"], env_label="本番", source="MCI Ver4")
        assert text == "[MCI Ver4][本番] 月次予測 失敗あり\na\nb"
        assert build_notify_text("t", [], env_label="STG") == "[STG] t"
        assert build_notify_text("t", ["x"]) == "t\nx"


# ---------------------------------------------------------------------------
# notify_error（設計書 4.1 / 5.2 / 7.1）
# ---------------------------------------------------------------------------
class TestNotifyError:
    def test_returns_false_and_sends_nothing_when_webhook_unset(self, caplog, test_logger):
        with patch.object(egpf_common.requests, "post") as post:
            assert notify_error("t", ["l"], logger=test_logger) is False
            post.assert_not_called()
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_blank_webhook_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL", "   ")
        with patch.object(egpf_common.requests, "post") as post:
            assert notify_error("t", ["l"]) is False
            post.assert_not_called()

    def test_sends_text_payload(self, monkeypatch, test_logger):
        monkeypatch.setenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL", "https://hooks.slack.test/services/X")
        with patch.object(egpf_common.requests, "post") as post:
            post.return_value = _resp(200, text="ok")
            ok = notify_error("月次予測 失敗あり", ["task_id: 1", "対象 1件 / 成功 0 / 失敗 1"],
                              env_label="STG", logger=test_logger, source="MCI Ver4")

        assert ok is True
        post.assert_called_once_with(
            "https://hooks.slack.test/services/X",
            json={"text": "[MCI Ver4][STG] 月次予測 失敗あり\ntask_id: 1\n対象 1件 / 成功 0 / 失敗 1"},
            timeout=10.0,
        )

    def test_env_label_defaults_to_env_var(self, monkeypatch):
        monkeypatch.setenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL", "https://hooks.slack.test/services/X")
        monkeypatch.setenv("ERROR_NOTIFY_ENV_LABEL", "本番")
        with patch.object(egpf_common.requests, "post") as post:
            post.return_value = _resp(200)
            notify_error("t", [], source="MCI Ver4")
        assert post.call_args.kwargs["json"]["text"] == "[MCI Ver4][本番] t"

    def test_webhook_500_logs_warning_and_returns_false(self, monkeypatch, caplog, test_logger):
        monkeypatch.setenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL", "https://hooks.slack.test/services/X")
        with patch.object(egpf_common.requests, "post") as post:
            post.return_value = _resp(500, text="internal error")
            assert notify_error("t", ["l"], logger=test_logger) is False

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "500" in warnings[0].getMessage()

    def test_post_exception_is_swallowed(self, monkeypatch, caplog, test_logger):
        monkeypatch.setenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL", "https://hooks.slack.test/services/X")
        with patch.object(egpf_common.requests, "post") as post:
            post.side_effect = requests.exceptions.ConnectionError("no route")
            assert notify_error("t", ["l"], logger=test_logger) is False

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "no route" in warnings[0].getMessage()
