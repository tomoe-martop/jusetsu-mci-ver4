# jusetsu-mci-ver4

## main2.pyの使用方法

`main2.py`は新しいMCI予測API（`api/pred_mci`）を使用して予測を実行するスクリプトです。

### 通常モード（DBからタスク取得→API取得→予測→DB保存）

DBから未処理のタスクを取得し、APIから電力データを取得して予測を実行し、結果をDBに保存します。

```
$ docker-compose run --rm python python3 main2.py
```

または、ローカルで直接実行する場合：

```
$ python3 main2.py
```

### CSVファイルから直接予測（API取得をスキップ）

APIが接続できない環境や、既存のCSVファイルから直接予測を実行する場合に使用します。

#### 基本的な使用方法

```bash
$ python3 main2.py --csv api/csv/test_data.csv --age 70 --male 0 --edu 12 --solo 1
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
$ python3 main2.py --csv api/csv/test_data.csv --age 75 --male 1 --edu 10 --solo 0 --debug
```

**Docker環境で実行:**
```bash
$ docker-compose run --rm python python3 main2.py --csv api/csv/test_data.csv --age 70 --male 0 --edu 12 --solo 1
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
