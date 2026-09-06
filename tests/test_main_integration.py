# -*- coding: utf-8 -*-
"""main.py の結合テスト（設計書 3.5 / 4.3 / 6.1）。

重い依存（mysql.connector / google.cloud / google.auth / pred_mci）を sys.modules にスタブして main() を
そのまま実行し、EGPF リトライ枯渇・打ち切り・N1/N2/N3 通知・DB 書き込み・ログ設定・GCS 退避を確認する。
ネットワーク・DB・GCP 資格情報は不要。実行: python -m pytest tests -q

前提:
- requests.get / requests.post / time.sleep は egpf_common 側でモックする
- /tmp/data/ 配下の CSV 出力と predictor.log の退避は tmp_path に向ける（実ファイルを消さない）
- main() は configure_logging() でルートロガーのハンドラを組み替えるため、テスト毎に元へ戻す
"""
import logging
import os
import sys
import types
from datetime import datetime as dt
from unittest.mock import MagicMock, patch

import pytest
import requests

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- 重い依存をスタブしてから main を import する --------------------------------
# GCP クライアント・MySQL ドライバは import 時に副作用が無いよう MagicMock に差し替える。
# （このテストモジュールを collection した以降、同じ pytest プロセスではスタブが残る）
_STUB_MODULES = (
    "mysql", "mysql.connector",
    "google", "google.cloud", "google.cloud.storage", "google.cloud.run_v2",
    "google.auth", "google.auth.transport", "google.auth.transport.requests",
    "google.oauth2", "google.oauth2.id_token",
    "pred_mci",
)
for _name in _STUB_MODULES:
    sys.modules[_name] = MagicMock(name=_name)
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

import main  # noqa: E402
import egpf_common  # noqa: E402

WEBHOOK = "https://hooks.slack.test/services/T/B/x"
GCS_BUCKET = "stg-mci-ver4"
DEFAULT_API_URL = "https://api.energy-gateway.jp/0.2/estimated_data"
DAY = ("2026-08-01", "2026-08-01")  # date_from, date_to（1 日分 = ハウス毎に GET 1 回）


def _house(task_house_id, houseid, spid="0187"):
    # task_houses の行: (id, spid, houseid, age, sex, education, solo)
    return (task_house_id, spid, houseid, 70, 1, 12, 1)


def _json_ok():
    return {"data": [{
        "timestamps": [1754000000 + 60 * i for i in range(3)],
        "appliance_types": [{"appliance_type_id": 2, "appliances": [{"powers": [1.0, 0.0, 2.0]}]}],
    }]}


def _resp(status, body=None):
    res = MagicMock(spec=requests.Response)
    res.status_code = status
    res.text = ""
    res.json.return_value = body if body is not None else _json_ok()
    return res


class FakeCursor:
    """main() が発行する SQL を記録し、SELECT にはタスク／ハウスの固定データを返す。"""

    def __init__(self, tasks, houses_by_task, fail_on=None):
        self.tasks = tasks                    # [(task_id, date_from, date_to)]
        self.houses_by_task = houses_by_task  # {task_id: [house row]}
        self.fail_on = fail_on                # callable(sql, params) -> Exception | None
        self.executed = []                    # [(空白正規化した SQL, params)]
        self._last = None

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.executed.append((sql, params))
        if self.fail_on is not None:
            exc = self.fail_on(sql, params)
            if exc is not None:
                raise exc
        self._last = (sql, params)

    def fetchall(self):
        sql, params = self._last
        if sql.startswith("SELECT id AS task_id"):
            return list(self.tasks)
        if sql.startswith("SELECT id AS task_house_id"):
            return list(self.houses_by_task.get(params[0], []))
        return []

    # --- 記録の読み出し ---
    def house_updates(self):
        """task_houses の更新を (task_house_id, status, progress) の順に返す。"""
        return [(p[2], p[0], p[1]) for s, p in self.executed if s.startswith("UPDATE `task_houses`")]

    def task_starts(self):
        return [p[0] for s, p in self.executed if s.startswith("UPDATE `tasks` SET start_at")]

    def task_ends(self):
        """tasks の終了更新を (status, task_id) の順に返す。"""
        return [p for s, p in self.executed if s.startswith("UPDATE `tasks` SET end_at")]

    def results(self):
        """task_results の INSERT を (task_id, task_house_id, result) の順に返す。"""
        return [p for s, p in self.executed if s.startswith("INSERT `task_results`")]

    def house_selects(self):
        return [p[0] for s, p in self.executed if s.startswith("SELECT id AS task_house_id")]


@pytest.fixture(autouse=True)
def restore_root_logger():
    """main.configure_logging() が付け替えるルートロガーのハンドラとレベルをテスト後に戻す。"""
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


@pytest.fixture
def env(monkeypatch, tmp_path):
    """main() が読む環境変数と、ファイル出力先（/tmp/data, predictor.log）を tmp_path に向ける。"""
    for name in ("EGPF_RETRY_MAX_ATTEMPTS", "EGPF_RETRY_WAIT_SEC", "EGPF_CONNECT_TIMEOUT_SEC",
                 "EGPF_READ_TIMEOUT_SEC", "EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES",
                 "MOCK_API_URL", "ENERGY_GATEWAY_API_URL",
                 "GOOGLE_CLOUD_PROJECT", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setenv("ERROR_NOTIFY_ENV_LABEL", "STG")
    monkeypatch.setenv("GCS_LOG_BUCKET", GCS_BUCKET)
    monkeypatch.setenv("API_SHARED_PASSWORD", "pw")
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    # predictor.log の探索先（base_dir / カレント）を tmp_path にする
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "base_dir", str(tmp_path))
    (tmp_path / "predictor.log").write_text("dummy log\n", encoding="utf-8")

    # /tmp/data/*.csv の書き出しと起動時クリーンアップを tmp_path/data に向ける
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    real_open = open

    def fake_open(path, *args, **kwargs):
        if isinstance(path, str) and path.startswith("/tmp/data/"):
            path = str(data_dir / os.path.basename(path))
        return real_open(path, *args, **kwargs)

    def fake_glob(pattern):
        assert pattern == "/tmp/data/*.csv"
        return [str(p) for p in data_dir.glob("*.csv")]

    monkeypatch.setattr(main, "open", fake_open, raising=False)
    monkeypatch.setattr(main, "glob", types.SimpleNamespace(glob=fake_glob))

    # GCS クライアントはテスト毎に新しいモック（呼び出し回数を数える）
    monkeypatch.setattr(main, "storage", MagicMock(name="storage"))
    return types.SimpleNamespace(tmp_path=tmp_path, data_dir=data_dir)


def _predictor(results):
    """get_predictor() の代わり。results は calculate_score の戻り値（dict）またはその list（ハウス順）。"""
    predictor = MagicMock(name="predictor")
    if isinstance(results, list):
        predictor.calculate_score.side_effect = results
    else:
        predictor.calculate_score.return_value = results
    return predictor


SCORE_OK = {"status_code": 100, "score": 60}


def run_main(cursor, get_side_effect, *, predictor=None, post_status=200, events=None):
    """main() を実行し (exit_code, get, post, sleep) を返す。events を渡すと GCS blob 作成と Slack POST の順序を記録する。"""
    cnx = MagicMock(name="cnx")
    cnx.cursor.return_value = cursor
    if predictor is None:
        predictor = _predictor(SCORE_OK)

    blob_mock = main.storage.Client.return_value.bucket.return_value.blob
    if events is not None:
        blob_instance = blob_mock.return_value
        blob_mock.side_effect = lambda name: events.append(("blob", name)) or blob_instance

    def fake_post(url, json=None, timeout=None, **kwargs):
        if events is not None:
            events.append(("post", json["text"]))
        return _resp(post_status, {})

    with patch.object(main.mysql.connector, "connect", return_value=cnx), \
            patch.object(main, "get_predictor", return_value=predictor), \
            patch.object(egpf_common.requests, "get", side_effect=get_side_effect) as get, \
            patch.object(egpf_common.requests, "post", side_effect=fake_post) as post, \
            patch.object(egpf_common.time, "sleep") as sleep:
        try:
            main.main()
            exit_code = 0
        except SystemExit as e:
            exit_code = e.code
    return exit_code, get, post, sleep


def _text(post, index=-1):
    return post.call_args_list[index].kwargs["json"]["text"]


def _log_blob_names(storage):
    blob = storage.Client.return_value.bucket.return_value.blob
    return [c.args[0] for c in blob.call_args_list if str(c.args[0]).startswith("logs/")]


# ---------------------------------------------------------------------------
# N1: タスク完了時に失敗ハウスあり
# ---------------------------------------------------------------------------
class TestN1TaskFailed:
    def test_notifies_failed_houses_with_category_and_log_path(self, env):
        cursor = FakeCursor([(433,) + DAY], {433: [_house(1, "H1"), _house(2, "H2"), _house(3, "H3")]})
        events = []

        code, get, post, sleep = run_main(cursor, [_resp(200)] + [_resp(503)] * 5 + [_resp(200)], events=events)

        assert code == 0
        assert get.call_count == 7
        assert sleep.call_count == 4
        # DB: H2 のみ -1（progress は EGPF 取得中の 10）、H1/H3 は成功
        assert (2, -1, 10) in cursor.house_updates()
        assert (1, 1, 100) in cursor.house_updates() and (3, 1, 100) in cursor.house_updates()
        assert cursor.results() == [(433, 1, 40), (433, 2, -1), (433, 3, 40)]  # 100 - score(60)
        assert cursor.task_ends() == [(1, 433)]
        # 通知は N1 が 1 通だけ
        assert post.call_count == 1
        text = _text(post)
        assert text.startswith("[MCI Ver4][STG] 月次予測 失敗あり\ntask_id: 433　実行: ")
        assert "対象 3件 / 成功 2 / 失敗 1" in text
        assert "失敗ハウス:\n - H2: EGPF通信エラー（5回送信して失敗: HTTP 503）\n" in text
        assert "ログ: gs://%s/logs/predictor_0000000433_" % GCS_BUCKET in text
        assert text.endswith("対応: ダッシュボード「頭の安心チェック」から再実行タスクを作成してください")
        # ログの GCS アップロードは 1 回だけで、通知はその後に送られる
        assert len(_log_blob_names(main.storage)) == 1
        assert [e[0] for e in events] == ["blob", "blob", "blob", "post"]
        assert events[2][1].startswith("logs/predictor_0000000433_")
        assert not (env.tmp_path / "predictor.log").exists()  # アップロード後にローカルは削除

    def test_multiple_failures_and_exception_categories(self, env):
        houses = [_house(1, "H1"), _house(2, "H2"), _house(3, "H3"), _house(4, "H4"), _house(5, "H5")]
        cursor = FakeCursor([(700,) + DAY], {700: houses})
        total_loss = {"data": [{"timestamps": [], "appliance_types": []}]}
        not_json = _resp(200)
        not_json.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "", 0)
        side = [_resp(404), _resp(200, total_loss), _resp(200, {"data": []}), not_json, _resp(200)]
        predictor = _predictor([{"status_code": 202}])  # H5 のみ予測まで到達

        code, get, post, sleep = run_main(cursor, side, predictor=predictor)

        assert code == 0
        assert get.call_count == 5 and sleep.call_count == 0  # 404 もデータ不備も再送しない
        text = _text(post)
        assert "対象 5件 / 成功 0 / 失敗 5" in text
        assert " - H1: EGPF応答エラー（HTTP 404）" in text
        assert " - H2: Total loss（ValueError: Total loss error!）" in text
        assert " - H3: データ無し（IndexError: list index out of range）" in text
        assert " - H4: データ無し（JSONDecodeError: Expecting value: line 1 column 1 (char 0)）" in text
        assert " - H5: 充足率不足（Exception: 必要な電力データ量を満たしていません）" in text
        # 設計書 4.5 の progress: 応答エラー/データ無し=10, Total loss=20, 充足率不足=30
        updates = cursor.house_updates()
        assert (1, -1, 10) in updates
        assert (2, -1, 20) in updates
        assert (3, -1, 10) in updates
        assert (4, -1, 10) in updates
        assert (5, -1, 30) in updates
        assert cursor.results() == [(700, i, -1) for i in range(1, 6)]
        assert cursor.task_ends() == [(1, 700)]

    @pytest.mark.parametrize("status_code, expected_line", [
        (302, " - H1: 予測処理エラー（Exception: 電力モデル予測時のエラー）"),
        (400, " - H1: 予測処理エラー（Exception: 予測時のタイムアウト）"),
        (900, " - H1: 予測処理エラー（Exception: 予期せぬエラー）"),
    ])
    def test_prediction_errors_are_classified_with_progress_30(self, env, status_code, expected_line):
        cursor = FakeCursor([(800,) + DAY], {800: [_house(1, "H1")]})

        code, get, post, sleep = run_main(cursor, [_resp(200)], predictor=_predictor({"status_code": status_code}))

        assert code == 0
        assert expected_line in _text(post)
        assert (1, -1, 30) in cursor.house_updates()
        assert cursor.results() == [(800, 1, -1)]

    def test_multi_day_range_calls_egpf_per_day(self, env):
        # 2 日分: 1 日目 200、2 日目で枯渇 → ハウス失敗（EGPF通信エラー、progress 10）
        cursor = FakeCursor([(810, "2026-08-01", "2026-08-02")], {810: [_house(1, "H1"), _house(2, "H2")]})
        side = [_resp(200)] + [requests.exceptions.ReadTimeout("read timed out")] * 5 + [_resp(200), _resp(200)]

        code, get, post, sleep = run_main(cursor, side)

        assert code == 0
        assert get.call_count == 8
        sts_day1 = int(dt.strptime("2026-08-01 00:00:00+0900", "%Y-%m-%d %H:%M:%S%z").timestamp())
        assert get.call_args_list[0].kwargs["params"]["sts"] == sts_day1
        assert get.call_args_list[1].kwargs["params"]["sts"] == sts_day1 + 86400
        assert get.call_args_list[1].kwargs["params"]["ets"] == sts_day1 + 2 * 86400
        assert all(c.kwargs["timeout"] == (10.0, 30.0) for c in get.call_args_list)
        assert " - H1: EGPF通信エラー（5回送信して失敗: read timeout）" in _text(post)
        assert (1, -1, 10) in cursor.house_updates()
        assert (2, 1, 100) in cursor.house_updates()

    def test_one_notification_per_failed_task(self, env):
        cursor = FakeCursor([(10,) + DAY, (11,) + DAY], {10: [_house(1, "A1")], 11: [_house(2, "B1")]})

        code, get, post, sleep = run_main(cursor, [_resp(404), _resp(200)])

        assert code == 0
        assert cursor.task_starts() == [10, 11]
        assert cursor.task_ends() == [(1, 10), (1, 11)]
        assert post.call_count == 1
        assert "task_id: 10　" in _text(post)
        assert " - A1: EGPF応答エラー（HTTP 404）" in _text(post)
        assert len(_log_blob_names(main.storage)) == 1


# ---------------------------------------------------------------------------
# N2: EGPF 全断の疑いによる打ち切り（設計書 3.5）
# ---------------------------------------------------------------------------
class TestN2Abort:
    def test_abort_after_3_consecutive_exhaustions_leaves_rest_untouched(self, env):
        houses = [_house(i, "H%d" % i) for i in range(1, 6)]
        cursor = FakeCursor([(500,) + DAY, (501,) + DAY], {500: houses, 501: [_house(9, "X1"), _house(10, "X2")]})
        events = []

        code, get, post, sleep = run_main(cursor, [_resp(503)] * 100, events=events)

        assert code == 0
        assert get.call_count == 15  # 3 ハウス × 5 試行
        assert sleep.call_count == 12
        updates = cursor.house_updates()
        # 処理した 3 ハウスは -1 確定、残り 2 ハウス（id 4, 5）は task_houses を触らない
        assert [u for u in updates if u[1] == -1] == [(1, -1, 10), (2, -1, 10), (3, -1, 10)]
        assert [u for u in updates if u[0] in (4, 5)] == []
        assert cursor.results() == [(500, 1, -1), (500, 2, -1), (500, 3, -1)]
        assert cursor.task_ends() == [(-1, 500)]
        # N2 が 1 通、ログの GCS パス付きでアップロード後に送られる
        assert post.call_count == 1
        text = _text(post)
        assert text.startswith("[MCI Ver4][STG] EGPF 障害の疑い（タスク打ち切り）\ntask_id: 500　実行: ")
        assert "対象 5件 / 成功 0 / 失敗 3 / 未処理 2" in text
        assert "EGPF への連続失敗によりタスクを打ち切りました（残り 2件は未処理。復旧後に再実行が必要）" in text
        assert " - H3: EGPF通信エラー（5回送信して失敗: HTTP 503）" in text
        assert "ログ: gs://%s/logs/predictor_0000000500_" % GCS_BUCKET in text
        assert "対応: ダッシュボード「頭の安心チェック」から再実行タスクを作成してください" in text
        assert [e[0] for e in events] == ["blob", "post"]
        assert len(_log_blob_names(main.storage)) == 1

    def test_abort_stops_subsequent_tasks_in_same_execution(self, env):
        """現状挙動の固定: 打ち切り後は同一実行内の後続タスクに着手しない（start_at も更新しない）。"""
        cursor = FakeCursor([(500,) + DAY, (501,) + DAY],
                            {500: [_house(i, "H%d" % i) for i in range(1, 5)], 501: [_house(9, "X1")]})

        code, get, post, sleep = run_main(cursor, [_resp(503)] * 100)

        assert code == 0
        assert cursor.task_starts() == [500]
        assert cursor.house_selects() == [500]
        assert cursor.task_ends() == [(-1, 500)]
        assert get.call_count == 15  # 3 ハウスで閾値到達、4 ハウス目は未処理
        assert post.call_count == 1 and "task_id: 500　" in _text(post)
        assert "未処理 1" in _text(post)

    def test_threshold_reached_on_last_house_completes_normally(self, env, capsys):
        # 最終ハウスで閾値到達（未処理 0 件）は打ち切りではなく通常完了: tasks.status=1・N1 通知・後続タスクも処理する
        cursor = FakeCursor([(505,) + DAY, (506,) + DAY],
                            {505: [_house(i, "H%d" % i) for i in range(1, 4)], 506: [_house(9, "X1")]})
        events = []

        code, get, post, sleep = run_main(cursor, [_resp(503)] * 15 + [_resp(200)], events=events)

        assert code == 0
        assert get.call_count == 16  # 505: 3 ハウス × 5 試行、506: 1 回で成功
        assert cursor.task_starts() == [505, 506]
        assert cursor.task_ends() == [(1, 505), (1, 506)]
        assert [u for u in cursor.house_updates() if u[1] == -1] == [(1, -1, 10), (2, -1, 10), (3, -1, 10)]
        assert (9, 1, 100) in cursor.house_updates()
        assert post.call_count == 1
        text = _text(post)
        assert text.startswith("[MCI Ver4][STG] 月次予測 失敗あり\ntask_id: 505　実行: ")
        assert "対象 3件 / 成功 0 / 失敗 3" in text
        assert "未処理" not in text and "打ち切り" not in text
        assert " - H3: EGPF通信エラー（5回送信して失敗: HTTP 503）" in text
        assert "ログ: gs://%s/logs/predictor_0000000506_" % GCS_BUCKET in text  # 実行末尾（最後のタスク）の退避ファイル
        # X1 の CSV → ログ退避 → N1 の順（通知はログ退避後）
        assert [e[0] for e in events] == ["blob", "blob", "post"]
        assert events[1][1].startswith("logs/predictor_0000000506_")
        out, err = capsys.readouterr()
        assert "Aborting task." not in out and "Aborting task." not in err

    def test_success_resets_consecutive_counter(self, env):
        houses = [_house(i, "H%d" % i) for i in range(1, 6)]
        cursor = FakeCursor([(600,) + DAY], {600: houses})
        # H1 枯渇, H2 枯渇, H3 成功(リセット), H4 枯渇, H5 枯渇 → 打ち切りなし
        side = [_resp(503)] * 5 + [_resp(503)] * 5 + [_resp(200)] + [_resp(503)] * 5 + [_resp(503)] * 5

        code, get, post, sleep = run_main(cursor, side)

        assert code == 0
        assert get.call_count == 21
        assert cursor.task_ends() == [(1, 600)]
        assert (3, 1, 100) in cursor.house_updates()
        assert post.call_count == 1
        text = _text(post)
        assert "月次予測 失敗あり" in text
        assert "対象 5件 / 成功 1 / 失敗 4" in text
        assert "未処理" not in text

    def test_counter_is_per_request_across_days(self, env):
        # 2 日分 × 3 ハウス。H1 の 1 日目成功で 0 に戻り、H1 2 日目・H2・H3 の連続 3 枯渇で打ち切り
        houses = [_house(1, "H1"), _house(2, "H2"), _house(3, "H3"), _house(4, "H4")]
        cursor = FakeCursor([(610, "2026-08-01", "2026-08-02")], {610: houses})
        side = [_resp(200)] + [_resp(503)] * 5 + [_resp(503)] * 5 + [_resp(503)] * 5 + [_resp(200)] * 10

        code, get, post, sleep = run_main(cursor, side)

        assert code == 0
        assert get.call_count == 16
        assert cursor.task_ends() == [(-1, 610)]
        assert [u for u in cursor.house_updates() if u[0] == 4] == []
        assert "対象 4件 / 成功 0 / 失敗 3 / 未処理 1" in _text(post)

    def test_client_errors_do_not_count_toward_abort(self, env):
        # 404 はリトライ枯渇ではないため打ち切りカウントに入らない（カウントすれば H3 で打ち切り＝未処理 3）が、
        # リセットもしない（リセットすれば打ち切りなし）。H1/H3/H5 の枯渇で H5 の時点で打ち切り、H6 は未処理
        houses = [_house(i, "H%d" % i) for i in range(1, 7)]
        cursor = FakeCursor([(620,) + DAY], {620: houses})
        side = [_resp(503)] * 5 + [_resp(404)] + [_resp(503)] * 5 + [_resp(404)] + [_resp(503)] * 5

        code, get, post, sleep = run_main(cursor, side)

        assert code == 0
        assert get.call_count == 17
        assert cursor.task_ends() == [(-1, 620)]
        assert [u for u in cursor.house_updates() if u[0] == 6] == []
        assert "対象 6件 / 成功 0 / 失敗 5 / 未処理 1" in _text(post)

    def test_abort_disabled_by_env_zero(self, env, monkeypatch):
        monkeypatch.setenv("EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES", "0")
        houses = [_house(i, "H%d" % i) for i in range(1, 5)]
        cursor = FakeCursor([(630,) + DAY], {630: houses})

        code, get, post, sleep = run_main(cursor, [_resp(503)] * 100)

        assert code == 0
        assert get.call_count == 20  # 4 ハウスすべて 5 試行
        assert cursor.task_ends() == [(1, 630)]
        assert "対象 4件 / 成功 0 / 失敗 4" in _text(post)
        assert "未処理" not in _text(post)

    def test_abort_log_goes_to_stderr(self, env, capsys):
        cursor = FakeCursor([(640,) + DAY], {640: [_house(i, "H%d" % i) for i in range(1, 5)]})

        run_main(cursor, [_resp(503)] * 100)

        out, err = capsys.readouterr()
        assert "Aborting task. task_id: 640, consecutive EGPF retry exhaustion. remaining houses: 1" in err
        assert "Aborting task." not in out


# ---------------------------------------------------------------------------
# 通知しない／送らないケース
# ---------------------------------------------------------------------------
class TestNoNotification:
    def test_no_failures_no_notification(self, env):
        cursor = FakeCursor([(900,) + DAY], {900: [_house(1, "H1"), _house(2, "H2")]})

        code, get, post, sleep = run_main(cursor, [_resp(200), _resp(200)])

        assert code == 0
        post.assert_not_called()
        assert cursor.task_ends() == [(1, 900)]
        assert cursor.results() == [(900, 1, 40), (900, 2, 40)]
        assert len(_log_blob_names(main.storage)) == 1

    def test_webhook_unset_sends_nothing_but_db_is_updated(self, env, monkeypatch):
        monkeypatch.delenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL")
        cursor = FakeCursor([(901,) + DAY], {901: [_house(1, "H1")]})

        code, get, post, sleep = run_main(cursor, [_resp(503)] * 5)

        assert code == 0
        post.assert_not_called()
        assert (1, -1, 10) in cursor.house_updates()
        assert cursor.results() == [(901, 1, -1)]
        assert cursor.task_ends() == [(1, 901)]

    def test_webhook_unset_abort_still_marks_task_failed(self, env, monkeypatch):
        monkeypatch.delenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL")
        cursor = FakeCursor([(902,) + DAY], {902: [_house(i, "H%d" % i) for i in range(1, 5)]})

        code, get, post, sleep = run_main(cursor, [_resp(503)] * 100)

        assert code == 0
        post.assert_not_called()
        assert cursor.task_ends() == [(-1, 902)]
        assert [u for u in cursor.house_updates() if u[0] == 4] == []

    def test_notification_failure_does_not_change_exit_code(self, env):
        cursor = FakeCursor([(903,) + DAY], {903: [_house(1, "H1")]})

        code, get, post, sleep = run_main(cursor, [_resp(404)], post_status=500)

        assert code == 0
        assert post.call_count == 1
        assert cursor.task_ends() == [(1, 903)]

    def test_no_tasks_exits_0_without_upload_or_notification(self, env):
        cursor = FakeCursor([], {})

        code, get, post, sleep = run_main(cursor, [])

        assert code == 0
        get.assert_not_called()
        post.assert_not_called()
        main.storage.Client.assert_not_called()
        assert (env.tmp_path / "predictor.log").exists()


# ---------------------------------------------------------------------------
# N3: タスク単位／最外殻の例外
# ---------------------------------------------------------------------------
class TestN3Unexpected:
    def test_outermost_exception_notifies_and_exits_1(self, env):
        with patch.object(main.mysql.connector, "connect", side_effect=RuntimeError("db unreachable")), \
                patch.object(egpf_common.requests, "get") as get, \
                patch.object(egpf_common.requests, "post", return_value=_resp(200, {})) as post:
            with pytest.raises(SystemExit) as ei:
                main.main()

        assert ei.value.code == 1
        get.assert_not_called()
        assert post.call_count == 1
        assert post.call_args.kwargs["json"] == {
            "text": "[MCI Ver4][STG] 予期しないエラー\n例外: RuntimeError: db unreachable\n処理を中断しました（exit 1）",
        }
        assert post.call_args.kwargs["timeout"] == 10.0
        main.storage.Client.assert_not_called()  # タスク処理前なのでログ退避もしない

    def test_outermost_exception_without_webhook_still_exits_1(self, env, monkeypatch):
        monkeypatch.delenv("ERROR_NOTIFY_SLACK_WEBHOOK_URL")
        with patch.object(main.mysql.connector, "connect", side_effect=RuntimeError("db unreachable")), \
                patch.object(egpf_common.requests, "post") as post:
            with pytest.raises(SystemExit) as ei:
                main.main()
        assert ei.value.code == 1
        post.assert_not_called()

    def test_task_level_exception_marks_task_failed_and_notifies(self, env):
        def fail_on(sql, params):
            if sql.startswith("SELECT id AS task_house_id") and params == (902,):
                return RuntimeError("select failed")
            return None

        cursor = FakeCursor([(902,) + DAY, (903,) + DAY], {903: [_house(1, "H1")]}, fail_on=fail_on)

        code, get, post, sleep = run_main(cursor, [_resp(200)])

        assert code == 0
        assert cursor.task_starts() == [902]  # 後続タスク 903 には着手しない
        assert cursor.task_ends() == [(-1, 902)]
        assert post.call_count == 1
        assert post.call_args.kwargs["json"]["text"] == (
            "[MCI Ver4][STG] 予期しないエラー\ntask_id: 902\n例外: RuntimeError: select failed")
        assert len(_log_blob_names(main.storage)) == 1  # 例外後もログは退避する

    def test_task_level_exception_does_not_lose_pending_n1(self, env):
        # タスク 1 の N1 は保留され、タスク 2 の N3 の後（ログ退避後）に送られる
        def fail_on(sql, params):
            if sql.startswith("SELECT id AS task_house_id") and params == (905,):
                return RuntimeError("select failed")
            return None

        cursor = FakeCursor([(904,) + DAY, (905,) + DAY], {904: [_house(1, "H1")]}, fail_on=fail_on)
        events = []

        code, get, post, sleep = run_main(cursor, [_resp(404)], events=events)

        assert code == 0
        assert post.call_count == 2
        assert _text(post, 0).startswith("[MCI Ver4][STG] 予期しないエラー\ntask_id: 905\n")
        assert _text(post, 1).startswith("[MCI Ver4][STG] 月次予測 失敗あり\ntask_id: 904　")
        assert "ログ: gs://" in _text(post, 1)
        assert [e[0] for e in events] == ["post", "blob", "post"]

    @pytest.mark.parametrize("post_status", [200, 500])
    def test_outermost_exception_flushes_pending_n1_before_n3(self, env, post_status):
        # タスク 1 の N1 が保留中に、タスク 2 の task 単位 except 内の UPDATE tasks で再度失敗 → 最外殻 except。
        # 保留中の N1 はログパス無しで N3 より先に送られ（送信失敗でも N3 へ進む）、終了コードは 1。
        # ログ退避は finally で行われる（task_id 不明）
        def fail_on(sql, params):
            if sql.startswith("SELECT id AS task_house_id") and params == (907,):
                return RuntimeError("select failed")
            if sql.startswith("UPDATE `tasks` SET end_at") and params == (-1, 907):
                return RuntimeError("db connection lost")
            return None

        cursor = FakeCursor([(906,) + DAY, (907,) + DAY], {906: [_house(1, "H1")]}, fail_on=fail_on)
        events = []

        code, get, post, sleep = run_main(cursor, [_resp(404)], events=events, post_status=post_status)

        assert code == 1
        assert cursor.task_ends() == [(1, 906), (-1, 907)]
        assert post.call_count == 2
        n1 = _text(post, 0)
        assert n1.startswith("[MCI Ver4][STG] 月次予測 失敗あり\ntask_id: 906　実行: ")
        assert "対象 1件 / 成功 0 / 失敗 1" in n1
        assert " - H1: EGPF応答エラー（HTTP 404）" in n1
        assert "ログ:" not in n1
        assert n1.endswith("対応: ダッシュボード「頭の安心チェック」から再実行タスクを作成してください")
        assert _text(post, 1) == (
            "[MCI Ver4][STG] 予期しないエラー\n例外: RuntimeError: db connection lost\n処理を中断しました（exit 1）")
        assert [e[0] for e in events] == ["post", "post", "blob"]
        assert events[2][1].startswith("logs/predictor_unknown_")


# ---------------------------------------------------------------------------
# spid 9991 のモック API 分岐（X-Serverless-Authorization）
# ---------------------------------------------------------------------------
class TestMockApiBranch:
    MOCK_URL = "https://mock-api-stg.example.test/0.2/estimated_data"

    def test_spid_9991_uses_mock_url_with_id_token_header(self, env, monkeypatch, capsys):
        monkeypatch.setenv("MOCK_API_URL", self.MOCK_URL)
        cursor = FakeCursor([(1000,) + DAY], {1000: [_house(1, "M1", spid="9991"), _house(2, "H1", spid="0187")]})

        with patch.object(main.google.oauth2.id_token, "fetch_id_token", return_value="tok123") as fetch:
            code, get, post, sleep = run_main(cursor, [_resp(200), _resp(200)])

        assert code == 0
        post.assert_not_called()
        assert fetch.call_count == 1
        assert fetch.call_args.args[1] == "https://mock-api-stg.example.test"  # audience はパス無しの origin
        mock_call, real_call = get.call_args_list
        assert mock_call.args[0] == self.MOCK_URL
        assert mock_call.kwargs["headers"] == {
            "Authorization": "imSP 9991:pw",
            "X-Serverless-Authorization": "Bearer tok123",
        }
        assert mock_call.kwargs["params"]["service_provider"] == "9991"
        assert mock_call.kwargs["params"]["house"] == "M1"
        assert mock_call.kwargs["params"]["time_units"] == 20
        assert mock_call.kwargs["timeout"] == (10.0, 30.0)
        assert real_call.args[0] == DEFAULT_API_URL
        assert real_call.kwargs["headers"] == {"Authorization": "imSP 0187:pw"}
        # 試行ログは main のロガー経由で標準出力に出る（設計書 3.3）
        out, _ = capsys.readouterr()
        assert "EGPF attempt=1/5 status=200 " in out
        assert "spid=9991 house=M1 sts=" in out

    def test_spid_9991_without_mock_url_uses_real_api(self, env):
        cursor = FakeCursor([(1001,) + DAY], {1001: [_house(1, "M1", spid="9991")]})

        with patch.object(main.google.oauth2.id_token, "fetch_id_token") as fetch:
            code, get, post, sleep = run_main(cursor, [_resp(200)])

        fetch.assert_not_called()
        assert get.call_args.args[0] == DEFAULT_API_URL
        assert "X-Serverless-Authorization" not in get.call_args.kwargs["headers"]

    def test_id_token_failure_continues_without_header(self, env, monkeypatch, capsys):
        monkeypatch.setenv("MOCK_API_URL", self.MOCK_URL)
        cursor = FakeCursor([(1002,) + DAY], {1002: [_house(1, "M1", spid="9991")]})

        with patch.object(main.google.oauth2.id_token, "fetch_id_token", side_effect=RuntimeError("no creds")):
            code, get, post, sleep = run_main(cursor, [_resp(200)])

        assert code == 0
        assert get.call_args.args[0] == self.MOCK_URL
        assert get.call_args.kwargs["headers"] == {"Authorization": "imSP 9991:pw"}
        assert (1, 1, 100) in cursor.house_updates()
        out, _ = capsys.readouterr()
        assert "ID token取得失敗(認証なしで継続): no creds" in out


# ---------------------------------------------------------------------------
# ログ設定（設計書 6.1「ログ設定」）とログ退避
# ---------------------------------------------------------------------------
class TestLoggingAndLogUpload:
    def test_log_level_applies_and_info_goes_to_stdout(self, env, capsys, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        cursor = FakeCursor([(903,) + DAY], {903: [_house(1, "H1")]})

        run_main(cursor, [_resp(503)] * 5)

        out, err = capsys.readouterr()
        assert "EGPF attempt=5/5 status=503" in out       # giving up は WARNING → 標準出力
        assert "EGPF attempt=1/5" not in out              # INFO は抑制される
        assert "Start main." not in out
        assert "category: EGPF通信エラー" in out
        assert "EGPF attempt" not in err

    def test_info_level_logs_each_attempt_to_stdout(self, env, capsys):
        cursor = FakeCursor([(904,) + DAY], {904: [_house(1, "H1")]})

        run_main(cursor, [_resp(503), _resp(200)])

        out, err = capsys.readouterr()
        assert "Start main." in out
        assert "EGPF attempt=1/5 status=503 " in out and "retry_in=2s" in out
        assert "EGPF attempt=2/5 status=200 " in out
        assert "Completed main." in out
        assert "Log file uploaded to gs://%s/logs/predictor_0000000904_" % GCS_BUCKET in out
        assert err == ""

    def test_root_handlers_are_stdout_stderr_plus_existing_file_handler(self, env, tmp_path):
        file_handler = logging.FileHandler(str(tmp_path / "keep.log"))
        root = logging.getLogger()
        root.addHandler(file_handler)
        try:
            main.configure_logging()
            handlers = root.handlers
            assert file_handler in handlers
            streams = [h for h in handlers if type(h) is logging.StreamHandler]
            assert len(streams) == 2
            assert {(h.stream, h.level) for h in streams} == {(sys.stdout, logging.INFO), (sys.stderr, logging.ERROR)}
            assert root.level == logging.INFO
            assert file_handler.level == logging.INFO
        finally:
            root.removeHandler(file_handler)
            file_handler.close()

    @pytest.mark.parametrize("log_level, root_level, file_level, stdout_level, stderr_level", [
        ("DEBUG", logging.DEBUG, logging.DEBUG, logging.DEBUG, logging.ERROR),
        ("INFO", logging.INFO, logging.INFO, logging.INFO, logging.ERROR),
        ("WARNING", logging.INFO, logging.INFO, logging.WARNING, logging.ERROR),
        ("ERROR", logging.INFO, logging.INFO, logging.ERROR, logging.ERROR),
        ("CRITICAL", logging.INFO, logging.INFO, logging.CRITICAL, logging.CRITICAL),
        ("bogus", logging.INFO, logging.INFO, logging.INFO, logging.ERROR),
    ])
    def test_log_level_applies_to_stdout_only_and_file_keeps_info(self, env, tmp_path, monkeypatch, log_level,
                                                                  root_level, file_level, stdout_level, stderr_level):
        # LOG_LEVEL は stdout/stderr にだけ効き、predictor.log 用 FileHandler は常に INFO 以上（DEBUG 時は DEBUG も）
        monkeypatch.setenv("LOG_LEVEL", log_level)
        file_handler = logging.FileHandler(str(tmp_path / "keep.log"))
        root = logging.getLogger()
        root.addHandler(file_handler)
        try:
            main.configure_logging()
            assert root.level == root_level
            assert file_handler.level == file_level
            levels = {h.stream: h.level for h in root.handlers if type(h) is logging.StreamHandler}
            assert levels == {sys.stdout: stdout_level, sys.stderr: stderr_level}
        finally:
            root.removeHandler(file_handler)
            file_handler.close()

    def test_invalid_log_level_falls_back_to_info_with_warning(self, env, monkeypatch, capsys):
        monkeypatch.setenv("LOG_LEVEL", "bogus")

        main.configure_logging()

        out, err = capsys.readouterr()
        assert "LOG_LEVEL='BOGUS' is not a valid level. using INFO" in out
        assert err == ""

    def test_file_handler_records_info_even_when_log_level_is_warning(self, env, tmp_path, monkeypatch, capsys):
        # 要件#102: 再送を含む通信ログは LOG_LEVEL=WARNING でも predictor.log（GCS 退避）に残る
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        keep_log = tmp_path / "keep.log"
        file_handler = logging.FileHandler(str(keep_log))
        root = logging.getLogger()
        root.addHandler(file_handler)
        cursor = FakeCursor([(908,) + DAY], {908: [_house(1, "H1")]})
        try:
            run_main(cursor, [_resp(503), _resp(200)])
        finally:
            root.removeHandler(file_handler)
            file_handler.close()

        out, err = capsys.readouterr()
        assert "EGPF attempt=1/5" not in out and "Start main." not in out
        logged = keep_log.read_text(encoding="utf-8")
        assert "Start main." in logged
        assert "EGPF attempt=1/5 status=503 " in logged and "retry_in=2s" in logged
        assert "EGPF attempt=2/5 status=200 " in logged
        assert "Completed main." in logged

    def test_log_saved_locally_when_bucket_unset(self, env, monkeypatch):
        monkeypatch.delenv("GCS_LOG_BUCKET")
        cursor = FakeCursor([(905,) + DAY], {905: [_house(1, "H1")]})

        code, get, post, sleep = run_main(cursor, [_resp(404)])

        assert code == 0
        main.storage.Client.assert_not_called()
        saved = list((env.tmp_path / "log").glob("predictor_0000000905_*.log"))
        assert len(saved) == 1
        assert "ログ: %s" % saved[0] in _text(post)

    def test_stale_csv_files_are_removed_at_start(self, env):
        # 起動時に前回実行の CSV を消してから、今回のハウス分を書き出す
        # （main.py は "/tmp/data/sample.csv" を文字列比較で除外するが、ここは tmp_path に向けているため対象外）
        stale = env.data_dir / "20260701_OLD_1.csv"
        stale.write_text("x", encoding="utf-8")
        cursor = FakeCursor([(906,) + DAY], {906: [_house(1, "H1")]})

        run_main(cursor, [_resp(200)])

        assert not stale.exists()
        written = list(env.data_dir.glob("20260801_H1_*.csv"))
        assert len(written) == 1
        header = written[0].read_text(encoding="utf-8").splitlines()[0]
        assert header == "date_time_jst,air_conditioner,clothes_washer,microwave,refrigerator,rice_cooker,TV,cleaner,IH,Heater"
