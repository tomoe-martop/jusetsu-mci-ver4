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
        assert "status=404" in lines[0] and lines[0].endswith("non-2xx, giving up")

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


# ---------------------------------------------------------------------------
# egpf_get: 境界・混在ケース（設計書 3.1 / 3.2 の追加検証）
# ---------------------------------------------------------------------------
def _status_labels(caplog):
    return [line.split()[2] for line in _attempt_lines(caplog)]


class TestEgpfGetBoundary:
    def test_total_wait_is_2s_times_4_for_5_attempts(self, mocked_io):
        get, sleep = mocked_io
        get.return_value = _resp(503)

        with pytest.raises(EgpfRetryExhausted):
            egpf_get(URL, HEADERS, PARAMS)

        assert get.call_count == 5
        assert sleep.call_count == 4  # 最後の試行の後は待たない
        assert sum(c.args[0] for c in sleep.call_args_list) == pytest.approx(8.0)

    def test_wait_from_env_applies_to_every_retry(self, mocked_io, monkeypatch, caplog, test_logger):
        monkeypatch.setenv("EGPF_RETRY_WAIT_SEC", "0.5")
        get, sleep = mocked_io
        get.return_value = _resp(502)

        with pytest.raises(EgpfRetryExhausted):
            egpf_get(URL, HEADERS, PARAMS, logger=test_logger)

        assert sleep.call_args_list == [call(0.5)] * 4
        assert sum(c.args[0] for c in sleep.call_args_list) == pytest.approx(2.0)
        assert all(line.endswith("retry_in=0.5s") for line in _attempt_lines(caplog)[:4])

    def test_negative_wait_is_clamped_to_zero(self, mocked_io, monkeypatch, caplog, test_logger):
        monkeypatch.setenv("EGPF_RETRY_WAIT_SEC", "-3")
        get, sleep = mocked_io
        get.side_effect = [_resp(503), _resp(200)]

        egpf_get(URL, HEADERS, PARAMS, logger=test_logger)

        assert get_retry_config()["wait_sec"] == 0.0
        assert sleep.call_args_list == [call(0.0)]
        assert _attempt_lines(caplog)[0].endswith("retry_in=0s")

    def test_max_attempts_1_means_no_retry(self, mocked_io, monkeypatch, caplog, test_logger):
        monkeypatch.setenv("EGPF_RETRY_MAX_ATTEMPTS", "1")
        get, sleep = mocked_io
        get.return_value = _resp(503)

        with pytest.raises(EgpfRetryExhausted) as ei:
            egpf_get(URL, HEADERS, PARAMS, context=CONTEXT, logger=test_logger)

        assert ei.value.attempts == 1
        assert ei.value.last_status == 503
        assert str(ei.value) == "EGPF request failed after 1 attempt(s): HTTP 503"
        assert get.call_count == 1
        sleep.assert_not_called()
        lines = _attempt_lines(caplog)
        assert len(lines) == 1
        assert lines[0].startswith("EGPF attempt=1/1 status=503 ")
        assert lines[0].endswith("giving up")

    @pytest.mark.parametrize("raw", ["0", "-2"])
    def test_max_attempts_below_1_is_clamped_to_1(self, mocked_io, monkeypatch, raw):
        monkeypatch.setenv("EGPF_RETRY_MAX_ATTEMPTS", raw)
        assert get_retry_config()["max_attempts"] == 1
        get, sleep = mocked_io
        get.return_value = _resp(500)

        with pytest.raises(EgpfRetryExhausted) as ei:
            egpf_get(URL, HEADERS, PARAMS)

        assert ei.value.attempts == 1
        assert get.call_count == 1
        sleep.assert_not_called()

    def test_mixed_5xx_and_429_then_200_on_last_attempt(self, mocked_io, caplog, test_logger):
        get, sleep = mocked_io
        ok = _resp(200)
        get.side_effect = [_resp(500), _resp(502), _resp(504), _resp(429), ok]

        assert egpf_get(URL, HEADERS, PARAMS, logger=test_logger) is ok

        assert get.call_count == 5
        assert sleep.call_args_list == [call(2.0)] * 4
        assert _status_labels(caplog) == ["status=500", "status=502", "status=504", "status=429", "status=200"]
        lines = _attempt_lines(caplog)
        assert lines[-1].startswith("EGPF attempt=5/5 status=200 ") and lines[-1].endswith(" ok")
        assert all(c.kwargs["timeout"] == (10.0, 30.0) for c in get.call_args_list)

    def test_mixed_timeout_connection_error_and_5xx_then_200(self, mocked_io, caplog, test_logger):
        get, sleep = mocked_io
        ok = _resp(200)
        get.side_effect = [
            requests.exceptions.ReadTimeout("read timed out"),
            _resp(503),
            requests.exceptions.ConnectionError("Connection reset by peer"),
            requests.exceptions.ConnectTimeout("connect timed out"),
            ok,
        ]

        assert egpf_get(URL, HEADERS, PARAMS, logger=test_logger) is ok

        assert get.call_count == 5
        assert sleep.call_count == 4
        assert _status_labels(caplog) == ["status=timeout", "status=503", "status=conn_error", "status=timeout", "status=200"]

    def test_last_error_and_status_reflect_the_final_attempt(self, mocked_io):
        get, sleep = mocked_io
        get.side_effect = [_resp(503)] * 4 + [requests.exceptions.ReadTimeout("read timed out")]
        with pytest.raises(EgpfRetryExhausted) as ei:
            egpf_get(URL, HEADERS, PARAMS)
        assert ei.value.last_status is None
        assert ei.value.last_error == "read timeout"

        get.side_effect = [requests.exceptions.ReadTimeout("read timed out")] * 4 + [_resp(502)]
        with pytest.raises(EgpfRetryExhausted) as ei:
            egpf_get(URL, HEADERS, PARAMS)
        assert ei.value.last_status == 502
        assert ei.value.last_error == "HTTP 502"

    def test_4xx_after_some_retries_gives_up_immediately(self, mocked_io):
        get, sleep = mocked_io
        get.side_effect = [_resp(503), _resp(503), _resp(401)]

        with pytest.raises(EgpfClientError) as ei:
            egpf_get(URL, HEADERS, PARAMS)

        assert ei.value.status == 401
        assert get.call_count == 3
        assert sleep.call_count == 2

    @pytest.mark.parametrize("status", [300, 301, 302, 304, 100])
    def test_non_2xx_non_retryable_statuses_raise_client_error(self, mocked_io, status):
        # 2xx / 429 / 5xx 以外はすべて「再送しない」側（リダイレクト等も含む）
        get, sleep = mocked_io
        get.return_value = _resp(status)

        with pytest.raises(EgpfClientError) as ei:
            egpf_get(URL, HEADERS, PARAMS)

        assert ei.value.status == status
        sleep.assert_not_called()

    @pytest.mark.parametrize("status", [200, 201, 204, 299])
    def test_any_2xx_is_returned(self, mocked_io, status):
        get, sleep = mocked_io
        res = _resp(status)
        get.return_value = res
        assert egpf_get(URL, HEADERS, PARAMS) is res
        sleep.assert_not_called()

    @pytest.mark.parametrize("exc", [
        requests.exceptions.ProxyError("proxy refused"),
        requests.exceptions.SSLError("handshake failed"),
        requests.exceptions.ConnectTimeout("connect timed out"),
    ])
    def test_connection_error_subclasses_are_retried(self, mocked_io, exc):
        get, sleep = mocked_io
        ok = _resp(200)
        get.side_effect = [exc, ok]

        assert egpf_get(URL, HEADERS, PARAMS) is ok
        assert get.call_count == 2
        assert sleep.call_args_list == [call(2.0)]

    @pytest.mark.parametrize("exc, expected", [
        (requests.exceptions.ConnectTimeout("connect timed out"), "connect timeout"),
        (requests.exceptions.ReadTimeout("read timed out"), "read timeout"),
        (requests.exceptions.Timeout("timed out"), "timeout"),
    ])
    def test_timeout_kind_is_reflected_in_last_error(self, mocked_io, exc, expected):
        get, sleep = mocked_io
        get.side_effect = exc

        with pytest.raises(EgpfRetryExhausted) as ei:
            egpf_get(URL, HEADERS, PARAMS)

        assert ei.value.last_error == expected
        assert ei.value.last_status is None
        assert get.call_count == 5

    def test_connection_error_message_is_one_line_and_truncated(self, mocked_io):
        get, sleep = mocked_io
        get.side_effect = requests.exceptions.ConnectionError("line1\nline2 " + "x" * 300)

        with pytest.raises(EgpfRetryExhausted) as ei:
            egpf_get(URL, HEADERS, PARAMS)

        assert "\n" not in ei.value.last_error
        assert ei.value.last_error.startswith("connection error: line1 line2 ")
        assert len(ei.value.last_error) <= len("connection error: ") + 120

    @pytest.mark.parametrize("exc", [
        requests.exceptions.TooManyRedirects("too many"),
        requests.exceptions.MissingSchema("no schema"),
    ])
    def test_non_connection_request_exceptions_propagate_without_retry(self, mocked_io, exc):
        get, sleep = mocked_io
        get.side_effect = exc

        with pytest.raises(type(exc)):
            egpf_get(URL, HEADERS, PARAMS)

        assert get.call_count == 1
        sleep.assert_not_called()

    # 本文受信中の切断（ChunkedEncodingError）・本文デコード失敗（ContentDecodingError）は
    # RequestException 直下で ConnectionError 派生ではないが、接続失敗と同列に再送する
    @pytest.mark.parametrize("exc_cls, message", [
        (requests.exceptions.ChunkedEncodingError, "Connection broken: IncompleteRead(512 bytes read, 1024 more expected)"),
        (requests.exceptions.ContentDecodingError, "Received response with content-encoding: gzip, but failed to decode it."),
    ])
    def test_body_receive_errors_are_retried_until_exhausted(self, mocked_io, caplog, test_logger, exc_cls, message):
        get, sleep = mocked_io
        get.side_effect = [exc_cls(message)] * 5

        with pytest.raises(EgpfRetryExhausted) as ei:
            egpf_get(URL, HEADERS, PARAMS, context=CONTEXT, logger=test_logger)

        exc = ei.value
        assert exc.attempts == 5
        assert exc.last_status is None
        assert exc.last_error == "%s: %s" % (exc_cls.__name__, message)
        assert exc.context == CONTEXT
        assert get.call_count == 5
        assert sleep.call_args_list == [call(2.0)] * 4
        lines = _attempt_lines(caplog)
        assert len(lines) == 5
        assert all("status=conn_error" in line for line in lines)
        assert lines[0].endswith("retry_in=2s") and lines[4].endswith("giving up")
        assert classify_failure(exc) == FAILURE_EGPF_COMM
        assert describe_failure(exc) == "5回送信して失敗: %s: %s" % (exc_cls.__name__, message)

    @pytest.mark.parametrize("exc_cls", [
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
    ])
    def test_body_receive_error_then_200_succeeds_on_second_attempt(self, mocked_io, caplog, test_logger, exc_cls):
        get, sleep = mocked_io
        ok = _resp(200)
        get.side_effect = [exc_cls("broken"), ok]

        assert egpf_get(URL, HEADERS, PARAMS, logger=test_logger) is ok
        assert get.call_count == 2
        assert sleep.call_args_list == [call(2.0)]
        lines = _attempt_lines(caplog)
        assert lines[0].startswith("EGPF attempt=1/5 status=conn_error ")
        assert lines[1].startswith("EGPF attempt=2/5 status=200 ") and lines[1].endswith(" ok")

    def test_body_receive_error_without_message_keeps_class_name(self, mocked_io):
        get, sleep = mocked_io
        get.side_effect = requests.exceptions.ChunkedEncodingError()

        with pytest.raises(EgpfRetryExhausted) as ei:
            egpf_get(URL, HEADERS, PARAMS)

        assert ei.value.last_error == "ChunkedEncodingError"

    def test_context_none_and_empty_do_not_break_logging(self, mocked_io, caplog, test_logger):
        get, sleep = mocked_io
        get.side_effect = [_resp(503), _resp(200)]

        egpf_get(URL, HEADERS, PARAMS, context=None, logger=test_logger)

        lines = _attempt_lines(caplog)
        assert len(lines) == 2
        assert lines[0].startswith("EGPF attempt=1/5 status=503 ") and lines[0].endswith("retry_in=2s")
        assert lines[1].startswith("EGPF attempt=2/5 status=200 ") and lines[1].endswith(" ok")

        caplog.clear()
        get.side_effect = None
        get.return_value = _resp(404)
        with pytest.raises(EgpfClientError) as ei:
            egpf_get(URL, HEADERS, PARAMS, context={}, logger=test_logger)
        assert ei.value.context == {}
        assert _attempt_lines(caplog)[0].endswith("non-2xx, giving up")

    def test_context_none_yields_empty_context_on_exhaustion(self, mocked_io):
        get, sleep = mocked_io
        get.return_value = _resp(503)

        with pytest.raises(EgpfRetryExhausted) as ei:
            egpf_get(URL, HEADERS, PARAMS)

        assert ei.value.context == {}

    def test_default_logger_is_module_logger(self, mocked_io, caplog):
        caplog.set_level(logging.INFO, logger="egpf_common")
        get, _ = mocked_io
        get.return_value = _resp(200)

        egpf_get(URL, HEADERS, PARAMS)

        assert any(r.name == "egpf_common" and r.getMessage().startswith("EGPF attempt=1/5 status=200 ")
                   for r in caplog.records)

    def test_headers_and_params_default_to_none(self, mocked_io):
        get, _ = mocked_io
        get.return_value = _resp(200)

        egpf_get(URL)

        get.assert_called_once_with(URL, headers=None, params=None, timeout=(10.0, 30.0))

    def test_timeout_tuple_order_is_connect_then_read(self, mocked_io, monkeypatch):
        monkeypatch.setenv("EGPF_CONNECT_TIMEOUT_SEC", "1")
        monkeypatch.setenv("EGPF_READ_TIMEOUT_SEC", "99")
        get, _ = mocked_io
        get.return_value = _resp(200)

        egpf_get(URL, HEADERS, PARAMS)

        assert get.call_args.kwargs["timeout"] == (1.0, 99.0)

    def test_guard_negative_threshold_is_disabled(self):
        guard = ConsecutiveFailureGuard(threshold=-1)
        assert guard.threshold == 0
        assert all(guard.record_exhausted() is False for _ in range(5))
        assert guard.consecutive_failures == 5


# ---------------------------------------------------------------------------
# notify_error: payload とタイムアウトの固定（設計書 4.4 / 5.2）
# ---------------------------------------------------------------------------
class TestNotifyErrorPayload:
    WEBHOOK = "https://hooks.slack.test/services/T/B/x"

    def test_payload_is_text_only_with_10s_timeout(self, monkeypatch):
        monkeypatch.setenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL", self.WEBHOOK)
        with patch.object(egpf_common.requests, "post") as post:
            post.return_value = _resp(200)
            assert notify_error("t", ["a", None, 3], env_label="STG", source="MCI Ver4") is True

        assert post.call_args.args == (self.WEBHOOK,)
        assert set(post.call_args.kwargs) == {"json", "timeout"}  # headers/data 等は付けない
        assert post.call_args.kwargs["json"] == {"text": "[MCI Ver4][STG] t\na\n3"}  # None は省き、非文字列は str 化
        assert post.call_args.kwargs["timeout"] == 10.0
        assert egpf_common.NOTIFY_TIMEOUT_SEC == 10.0

    def test_webhook_url_is_stripped(self, monkeypatch):
        monkeypatch.setenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL", "  %s \n" % self.WEBHOOK)
        with patch.object(egpf_common.requests, "post") as post:
            post.return_value = _resp(200)
            notify_error("t", [])
        assert post.call_args.args == (self.WEBHOOK,)

    def test_explicit_env_label_overrides_env_var(self, monkeypatch):
        monkeypatch.setenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL", self.WEBHOOK)
        monkeypatch.setenv("ERROR_NOTIFY_ENV_LABEL", "本番")
        with patch.object(egpf_common.requests, "post") as post:
            post.return_value = _resp(200)
            notify_error("t", [], env_label="STG")
        assert post.call_args.kwargs["json"]["text"] == "[STG] t"

    def test_blank_env_label_var_is_omitted(self, monkeypatch):
        monkeypatch.setenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL", self.WEBHOOK)
        monkeypatch.setenv("ERROR_NOTIFY_ENV_LABEL", "")
        with patch.object(egpf_common.requests, "post") as post:
            post.return_value = _resp(200)
            notify_error("t", ["x"], source="MCI Ver4")
        assert post.call_args.kwargs["json"]["text"] == "[MCI Ver4] t\nx"

    @pytest.mark.parametrize("status, expected", [(200, True), (204, True), (299, True), (301, False), (404, False)])
    def test_only_2xx_counts_as_sent(self, monkeypatch, status, expected):
        monkeypatch.setenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL", self.WEBHOOK)
        with patch.object(egpf_common.requests, "post") as post:
            post.return_value = _resp(status, text="body")
            assert notify_error("t", ["x"]) is expected

    def test_post_timeout_is_swallowed_with_warning(self, monkeypatch, caplog, test_logger):
        monkeypatch.setenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL", self.WEBHOOK)
        with patch.object(egpf_common.requests, "post") as post:
            post.side_effect = requests.exceptions.Timeout("slack timed out")
            assert notify_error("t", ["x"], logger=test_logger) is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "slack timed out" in warnings[0].getMessage()


class TestFormatTaskSummaryBoundary:
    def test_period_with_only_one_end(self):
        assert format_task_summary(1, datetime(2026, 8, 2, 0, 0), None, 1, 1, [])[0] == "task_id: 1　実行: 2026-08-02 00:00〜"
        assert format_task_summary(1, None, datetime(2026, 8, 2, 0, 47), 1, 1, [])[0] == "task_id: 1　実行: 〜2026-08-02 00:47"

    def test_remaining_zero_still_reports_abort(self):
        lines = format_task_summary(2, None, None, 3, 0, [("A", FAILURE_EGPF_COMM, "5回送信して失敗: HTTP 503")] * 3,
                                    remaining=0, log_path="gs://b/logs/x.log", action="act")
        assert lines[1] == "対象 3件 / 成功 0 / 失敗 3 / 未処理 0"
        assert lines[2] == "EGPF への連続失敗によりタスクを打ち切りました（残り 0件は未処理。復旧後に再実行が必要）"
        assert lines[-2:] == ["ログ: gs://b/logs/x.log", "対応: act"]

    def test_failures_accepts_any_sequence_and_ids(self):
        lines = format_task_summary(3, None, None, 2, 0, (("H1", FAILURE_OTHER, ""), (42, FAILURE_TOTAL_LOSS, "d")))
        assert " - H1: その他" in lines
        assert " - 42: Total loss（d）" in lines
