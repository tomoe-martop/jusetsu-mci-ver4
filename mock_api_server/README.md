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
GET http://localhost:3000/0.2/estimated_data
```

### クエリパラメータ

| パラメータ | 必須 | 説明 | 例 |
|-----------|------|------|-----|
| service_provider | ✓ | サービスプロバイダID（9991固定） | 9991 |
| house | ✓ | ハウスID | DUMMY00001 |
| sts | ✓ | 開始日時（Unix timestamp） | 1718294400 |
| ets | ✓ | 終了日時（Unix timestamp） | 1718380800 |
| time_units | - | 時間単位 | 20 |

### ハウスIDとCSVファイルの対応

| ハウスID | CSVファイル | 説明 |
|---------|------------|------|
| DUMMY00001 | 202508_001.csv | 2025年8月のデータ（001） |
| DUMMY00002 | 202508_002.csv | 2025年8月のデータ（002） |
| DUMMY00025 | 202508_025.csv | 2025年8月のデータ（025） |

ファイル名の規則: `YYYYMM_XXX.csv`
- YYYYMM: 年月（stsから自動判定）
- XXX: ハウスIDから抽出（DUMMY00001 → 001）

### リクエスト例

```bash
# 2024年6月14日 00:00:00 ～ 2024年6月15日 00:00:00 のデータを取得
curl "http://localhost:3000/0.2/estimated_data?service_provider=9991&house=DUMMY00001&sts=1718294400&ets=1718380800&time_units=20"
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
    'house': 'DUMMY00001',
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
curl "https://mock-api-stg-xxxxx-an.a.run.app/0.2/estimated_data?service_provider=9991&house=DUMMY00001&sts=1718336280&ets=1718336400&time_units=20"
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