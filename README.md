# jusetsu-mci-ver4

## main.pyの使用方法

`main.py`は新しいMCI予測API（`api/pred_mci`）を使用して予測を実行するスクリプトです。

### 通常モード（DBからタスク取得→API取得→予測→DB保存）

DBから未処理のタスクを取得し、APIから電力データを取得して予測を実行し、結果をDBに保存します。

```
$ docker-compose run --rm python python3 main.py
```

または、ローカルで直接実行する場合：

```
$ python3 main.py
```

### CSVファイルから直接予測（API取得をスキップ）

APIが接続できない環境や、既存のCSVファイルから直接予測を実行する場合に使用します。

#### 基本的な使用方法

```bash
$ python3 main.py --csv api/csv/test_data.csv --age 70 --male 0 --edu 12 --solo 1
```

#### 引数オプション

- `--csv` / `-c`: CSVファイルパス（必須。指定した場合はAPI取得をスキップして直接予測を実行）
- `--age`: 年齢（デフォルト: 70）
- `--male`: 性別（男性=1、女性=0、デフォルト: 0）
- `--edu`: 教育年数（デフォルト: 12）
- `--solo`: 独居（独居=1、同居者あり=0、デフォルト: 1）
- `--debug`: デバッグモード（詳細な予測値を表示）
- `--task-id`: タスクID（CSV実行でのDB保存時に指定）
- `--task-house-id`: タスクハウスID（CSV実行でのDB保存時に指定）

#### 使用例

**デバッグモードで実行:**
```bash
$ python3 main.py --csv api/csv/test_data.csv --age 75 --male 1 --edu 10 --solo 0 --debug
```

**Docker環境で実行:**
```bash
$ docker-compose run --rm python python3 main.py --csv api/csv/test_data.csv --age 70 --male 0 --edu 12 --solo 1
```

## ログファイルの確認方法

Docker環境で実行した場合、`predictor.log`は以下の場所に保存されます。

### ホスト側（ローカル）から確認

`docker-compose.yml`で`volumes: - .:/tmp`とマウントされているため、コンテナ内の`/tmp/log/predictor.log`はホスト側の`log/predictor.log`に対応しています。

```bash
# ホスト側から直接確認（最も簡単）
cat log/predictor.log

# 末尾を確認
tail -f log/predictor.log

# ファイルサイズを確認
ls -lh log/predictor.log
```

### コンテナ内から確認

```bash
# コンテナ内でファイルを確認
docker-compose run --rm python cat /tmp/log/predictor.log

# コンテナ内でファイルリストを確認
docker-compose run --rm python ls -lh /tmp/log/predictor.log

# 実行中のコンテナがある場合
docker exec -it <コンテナ名> cat /tmp/log/predictor.log
```

### ログファイルの場所

- **ホスト側**: `プロジェクトルート/log/predictor.log`
- **コンテナ内**: `/tmp/log/predictor.log`

両者はボリュームマウントで同期されています。

## EGPF連携のリトライとエラー通知

EGPF（Energy Gateway）API の呼び出しは共通モジュール `api/egpf_common.py` の `egpf_get()` を経由し、通信系エラーを自動で再送します。タスク完了時に失敗ハウスがあれば Slack へ通知します。

- 仕様の正: `docs/EGPF連携リトライ・エラー通知対応/設計書_リトライ共通仕様・通知設計.md`（ワークスペース直下の `docs/`。Backlog EG_DASHBOARD-93）
- `api/egpf_common.py` は **jusetsu-mci-ver3（Python 3.8）／jusetsu-csv-function と同一内容をコピー配置**しています。変更時は `__version__` を上げ、3 リポジトリすべてに反映してください（Python 3.8 互換・依存は `requests` と標準ライブラリのみ）

### リトライ仕様（設計書 3 章）

| 事象 | 扱い |
|---|---|
| 接続失敗・接続／読み取りタイムアウト・HTTP 5xx・HTTP 429 | 再送（最大 5 回送信、固定 2 秒待ち）。枯渇したら `EgpfRetryExhausted` |
| HTTP 4xx（429 以外） | 再送しない。即 `EgpfClientError` |
| 2xx だが `data` が空／Total loss／充足率不足／予測処理の例外 | 再送しない（従来どおりハウス失敗 `status=-1`） |

- 試行ごとに `EGPF attempt=2/5 status=503 elapsed=0.41s spid=... house=... sts=... ets=... retry_in=2s` の形式で INFO ログを出します（`predictor.log` と Cloud Logging の両方）
- **打ち切り**: 連続 3 リクエストでリトライが枯渇した場合は EGPF 障害の疑いとしてタスクを打ち切ります。処理中のハウスは `-1` 確定、残りのハウスは未処理のまま（`task_houses` は更新しない）、`tasks.status=-1` を書いて通知します。復旧後はダッシュボードから再実行タスクを作成してください

### エラー通知（設計書 4 章）

`ERROR_NOTIFY_SLACK_WEBHOOK_URL` が未設定なら通知は行わず、動作は従来どおりです。通知は Slack Incoming Webhook へ `{"text": ...}` のみの単純 payload で送ります。通知の送信失敗は warning ログのみで、本処理は止めません。

| # | イベント | タイミング |
|---|---|---|
| N1 | タスク完了時に失敗ハウスあり（失敗 0 件なら通知しない） | ログの GCS アップロード後（本文にログのパスを含める） |
| N2 | EGPF 全断によるタスク打ち切り | 同上 |
| N3 | タスク単位／最外殻の例外（DB 接続失敗等） | 発生時 |

本文の例（失敗ハウスの列挙は最大 20 行、超過分は「他 N 件」）:

```
[MCI Ver4][本番] 月次予測 失敗あり
task_id: 433　実行: 2026-08-02 00:00〜00:47
対象 12件 / 成功 10 / 失敗 2
失敗ハウス:
 - DUSKIN0001: EGPF通信エラー（5回送信して失敗: read timeout）
 - DUSKIN0003: EGPF通信エラー（5回送信して失敗: HTTP 503）
ログ: gs://prd-mci-ver4/logs/predictor_0000000433_20260802004700.log
対応: ダッシュボード「頭の安心チェック」から再実行タスクを作成してください
```

失敗分類（ログ・通知にのみ載せ、DB には書きません）: `EGPF通信エラー` / `EGPF応答エラー` / `データ無し` / `Total loss` / `充足率不足` / `予測処理エラー` / `その他`

### 環境変数

| 変数 | 既定 | 内容 |
|---|---|---|
| `EGPF_RETRY_MAX_ATTEMPTS` | 5 | 最大送信回数（初回 1＋再送 4） |
| `EGPF_RETRY_WAIT_SEC` | 2 | 再送間隔（秒、固定） |
| `EGPF_CONNECT_TIMEOUT_SEC` | 10 | 接続タイムアウト（秒） |
| `EGPF_READ_TIMEOUT_SEC` | 30 | 読み取りタイムアウト（秒） |
| `EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES` | 3 | 連続リトライ枯渇の打ち切り閾値（0 で無効） |
| `ERROR_NOTIFY_SLACK_WEBHOOK_URL` | （未設定） | Slack Incoming Webhook URL。未設定なら通知しない |
| `ERROR_NOTIFY_ENV_LABEL` | deploy.sh が `本番`（`ENV=prd`）／`STG` を設定 | 通知の先頭に付く環境ラベル |
| `LOG_LEVEL` | INFO（main.py）／ERROR（deploy.sh） | ログレベル。リトライの試行ログは INFO のため、確認したい場合は `INFO` を設定する |

`bin/deploy.sh` は `.env.stg` / `.env.prd` から上記を読み、`gcloud run jobs deploy --set-env-vars` に載せます（`EGPF_*` は設定されている変数だけ）。`--set-env-vars` はカンマ区切りのため、**値にカンマを含めないでください**。Webhook URL はシークレットとして扱い、コミットしないでください。

### 単体テストの実行

`tests/` に共通モジュールの pytest があります（`requests.get` / `requests.post` / `time.sleep` はモックのため、ネットワーク・DB は不要）。

```bash
pip install pytest requests
python -m pytest tests -q
```

### STG 結合試験（モックサーバの障害注入）

spid `9991` のタスクは `MOCK_API_URL`（`mock_api_server`）に向きます。モックサーバは環境変数 `MOCK_FAIL_STATUS` / `MOCK_FAIL_COUNT` / `MOCK_DELAY_MS` で 5xx や応答遅延を注入できます。詳細は `mock_api_server/README.md` を参照してください。

## main.pyの実行方法（旧版）

### 単発での実行方法
ローカルで。
```
$ docker-compose run --rm python
```
サーバーで。
```
$ sudo docker-compose run --rm python
```

## 実行を継続する場合は、cronを利用
```
$ chmod 755 main.sh
$ sudo cp /etc/crontab /etc/cron.d/crontab
$ sudo vi /etc/cron.d/crontab
```
### 設定例(debianの場合)
```
* * * * * ユーザー名 /bin/bash /opt/jusetsu-mci-ver2/main.sh >> /opt/jusetsu-mci-ver2/log/cron.log 2>&1
```
### 念の為cron再起動
```
$ sudo service cron restart
```
