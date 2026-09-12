# Energy Gateway Mock API Server

Energy Gateway APIのモックサーバーです。`test_api_data`ディレクトリのCSVファイルを読み込んで、APIのJSON形式でデータを返します。

ローカル環境またはCloud Runで動作します。

## セットアップ

### 1. 依存関係のインストール

```bash
cd mock_api_server
npm install
```

### 2. サーバーの起動

```bash
npm start
```

開発時（ファイル変更で自動再起動）:
```bash
npm run dev
```

サーバーは http://localhost:3000 で起動します。

## 使い方

### エンドポイント

```
GET http://localhost:3000/0.2/estimated_data     # MCI Ver3/Ver4 が使用（CSV/DB のデータを返す）
GET http://localhost:3000/0.2/calculated_data    # 見える化CSV が使用（固定データ。下記参照）
GET http://localhost:3000/0.2/observed_data      # 見える化CSV が使用（固定データ。下記参照）
```

### クエリパラメータ

| パラメータ | 必須 | 説明 | 例 |
|-----------|------|------|-----|
| service_provider | ✓ | サービスプロバイダID（9991固定） | 9991 |
| house | ✓ | ハウスID | 2025080001 |
| sts | ✓ | 開始日時（Unix timestamp） | 1718294400 |
| ets | ✓ | 終了日時（Unix timestamp） | 1718380800 |
| time_units | - | 時間単位 | 20 |

### ハウスIDとCSVファイルの対応

| ハウスID | CSVファイル | 説明 |
|---------|------------|------|
| 2025080001 | 202508_001.csv | 2025年8月のデータ（001） |
| 2025080002 | 202508_002.csv | 2025年8月のデータ（002） |
| 2025080095 | 202508_095.csv | 2025年8月のデータ（095） |

ファイル名の規則: `YYYYMM_XXX.csv`
- YYYYMM: 年月
- XXX: ハウスIDから抽出（2025080001 → 001）

### リクエスト例

```bash
# 2024年6月14日 00:00:00 ～ 2024年6月15日 00:00:00 のデータを取得
curl "http://localhost:3000/0.2/estimated_data?service_provider=9991&house=2025080001&sts=1718294400&ets=1718380800&time_units=20"
```

### Pythonからの使用例

```python
import requests
from datetime import datetime

url = "http://localhost:3000/0.2/estimated_data"

# 期間を指定
sts = int(datetime(2024, 6, 14, 0, 0, 0).timestamp())
ets = int(datetime(2024, 6, 15, 0, 0, 0).timestamp())

params = {
    'service_provider': 9991,
    'house': '2025080001',
    'sts': sts,
    'ets': ets,
    'time_units': 20
}

headers = {'Authorization': 'imSP 9991:password'}

response = requests.get(url, headers=headers, params=params)
print(response.json())
```

## レスポンス形式

```json
{
  "data": [
    {
      "timestamps": [1718294400, 1718294460, 1718294520, ...],
      "appliance_types": [
        {
          "appliance_type_id": 2,
          "appliances": [
            {
              "powers": [4.052, 6.000, null, ...]
            }
          ]
        },
        {
          "appliance_type_id": 5,
          "appliances": [
            {
              "powers": [0.0, 0.0, null, ...]
            }
          ]
        }
      ]
    }
  ]
}
```

### 家電タイプID

| ID | 家電名 | CSVカラム名 |
|----|--------|------------|
| 2 | エアコン | air_conditioner |
| 5 | 洗濯機 | clothes_washer |
| 20 | 電子レンジ | microwave |
| 24 | 冷蔵庫 | refrigerator |
| 25 | 炊飯器 | rice_cooker |
| 30 | テレビ | TV |
| 31 | 掃除機 | cleaner |
| 37 | IH | IH |
| 301 | ヒーター | Heater |

## 見える化CSV 向けエンドポイント（calculated_data / observed_data）

見える化CSV（`jusetsu-csv-function`）は `estimated_data` 以外に計算値（`calculated_data`）と実測値（`observed_data`）も呼びます。STG 試験（設計書 7.2 の S1/S6）は障害注入が目的なので、この2本は**固定の擬似データ**を返します（CSV/DB は参照しません）。

- `service_provider` は値を検証しません（`estimated_data` の 9991 固定チェックは適用されません）。STG の事業者IDをそのまま指定できます
- `timestamps` は `sts` / `ets` / `time_units`（20:分 30:時 40:日 50:月 60:年）から生成します。最大 2000 点で打ち切ります
- `calculated_data` が返す `appliance_id` は既定で `1,2,3,4,5,8,9`（発電量・充電量・買電量・総消費電力・自家消費量・放電量・売電量）。`MOCK_CALCULATED_APPLIANCE_IDS` で変更できます
- `observed_data` は上記「家電タイプID」の全タイプについて `appliance_id=1` の系列を1本返します
- `upload_ratio` / `voltage_data` は未実装です。これらを使う出力条件（電圧・通信率）は試験対象外にしてください

```bash
curl "http://localhost:3000/0.2/calculated_data?service_provider=1234&house=H1&sts=1718294400&ets=1718305200&time_units=30"
```

## 障害注入（STG 試験用）

EGPF 障害を疑似的に再現するため、`/0.2/` 配下の**全エンドポイント**（`estimated_data` / `calculated_data` / `observed_data`）に環境変数で失敗・遅延を注入できます。未設定なら通常どおり応答します。

| 環境変数 | 説明 | 例 |
|---|---|---|
| `MOCK_FAIL_STATUS` | 設定時、この HTTP ステータスで失敗させる。未設定または 0 なら無効 | `503` |
| `MOCK_FAIL_COUNT` | 最初の N リクエストだけ失敗させる。0 または未設定なら常時失敗 | `2` |
| `MOCK_DELAY_MS` | 応答前に待つミリ秒（read timeout の再現用。失敗注入と併用可） | `40000` |

- 失敗回数のカウンタは **プロセス（Cloud Run のインスタンス）単位**、かつ **エンドポイント横断で共通** です（`MOCK_FAIL_COUNT=2` なら最初の2リクエストが、どのエンドポイントでも失敗します）。Cloud Run で `MOCK_FAIL_COUNT` を使う場合はインスタンスが複数立たないよう `--max-instances 1` にするか、常時失敗（`MOCK_FAIL_COUNT` 未設定）で試験してください
- 現在の設定と注入回数は `GET /health` の `faultInjection` で確認できます

ローカルでの例:

```bash
# 503 を 2 回返してから正常応答（リトライで成功するケース）
MOCK_FAIL_STATUS=503 MOCK_FAIL_COUNT=2 npm start

# 503 固定（リトライ枯渇・打ち切りのケース）
MOCK_FAIL_STATUS=503 npm start

# 40 秒遅延（read timeout 30 秒のケース）
MOCK_DELAY_MS=40000 npm start
```

Cloud Run（`mock-api-stg`）での例:

```bash
# 注入を有効化
gcloud run services update mock-api-stg --region asia-northeast1 \
  --update-env-vars MOCK_FAIL_STATUS=503,MOCK_FAIL_COUNT=2

# 注入を解除
gcloud run services update mock-api-stg --region asia-northeast1 \
  --remove-env-vars MOCK_FAIL_STATUS,MOCK_FAIL_COUNT,MOCK_DELAY_MS
```

※ `deploy.sh` は `--set-env-vars` で環境変数を丸ごと置き換えるため、再デプロイすると注入設定は消えます。

## Unix Timestamp変換

日時をUnix timestampに変換するツール:

```bash
# macOS/Linux
date -j -f "%Y-%m-%d %H:%M:%S" "2024-06-14 00:00:00" +%s

# オンラインツール
# https://www.unixtimestamp.com/
```

## トラブルシューティング

### ポート3000が使用中の場合

server.jsの`PORT`を変更してください:

```javascript
const PORT = 3001; // 任意のポートに変更
```

### CSVファイルが見つからない場合

エラーメッセージで確認されるファイルパスをチェックしてください:
```
Looking for CSV file: /path/to/test_api_data/202508_001.csv
```

`test_api_data`ディレクトリが`mock_api_server`と同じ階層にあることを確認してください。

## Cloud Runへのデプロイ

モックAPIサーバーをCloud Runにデプロイできます。

### 前提条件

- Google Cloud SDKがインストールされていること
- GCPプロジェクトが設定されていること
- 必要なAPIが有効化されていること
  - Cloud Run API
  - Cloud Build API

### デプロイ手順

```bash
cd mock_api_server
./deploy.sh
```

デプロイ後、サービスURLが表示されます：
```
URL: https://mock-api-stg-xxxxx-an.a.run.app
```

### Cloud Run版の使用例

```bash
# デプロイ後のURLを使用
curl "https://mock-api-stg-xxxxx-an.a.run.app/0.2/estimated_data?service_provider=9991&house=2025080001&sts=1718336280&ets=1718336400&time_units=20"
```

### Cloud Runの設定

deploy.shで以下の設定を使用しています：
- メモリ: 512Mi
- CPU: 1
- 最大インスタンス数: 10
- 最小インスタンス数: 0（自動スケールダウン）
- 認証: なし（--allow-unauthenticated）

必要に応じてdeploy.shを編集してください。

## ディレクトリ構成

```
jusetsu-mci-ver4/
└── mock_api_server/
    ├── package.json
    ├── package-lock.json
    ├── server.js
    ├── Dockerfile
    ├── .dockerignore
    ├── deploy.sh
    ├── README.md
    └── test_api_data/
        ├── 202508_001.csv
        ├── 202508_002.csv
        └── ...
```