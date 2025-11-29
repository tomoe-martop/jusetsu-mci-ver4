# モックデータ DBインポート手順

CSVデータをCloud SQL（stg）にインポートする手順です。

## 前提条件

- Google Cloud Shell へのアクセス
- Cloud SQL インスタンスへの接続権限
- `mock_energy_data` テーブルが作成済み

## テーブル作成（初回のみ）

stg DBで以下のSQLを実行：

```sql
CREATE TABLE mock_energy_data (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  house_id VARCHAR(20) NOT NULL,
  timestamp INT NOT NULL,
  air_conditioner FLOAT,
  clothes_washer FLOAT,
  microwave FLOAT,
  refrigerator FLOAT,
  rice_cooker FLOAT,
  TV FLOAT,
  cleaner FLOAT,
  IH FLOAT,
  Heater FLOAT,
  INDEX idx_house_timestamp (house_id, timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

## インポート手順

### 1. Cloud Shellを開く

Google Cloud Console → 右上のCloud Shellアイコンをクリック

### 2. ファイルをアップロード

Cloud Shellの右上メニュー「︙」→「アップロード」から以下をアップロード：
- `test_api_data/` フォルダ（CSVファイル一式）
- `import_to_db.js`

### 3. 依存パッケージをインストール

```bash
cd ~
npm init -y && npm install mysql2 csv-parser moment
```

### 4. Cloud SQL Proxyをダウンロード

```bash
curl -o cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.8.0/cloud-sql-proxy.linux.amd64
chmod +x cloud-sql-proxy
```

### 5. 環境変数を設定

```bash
export CLOUD_SQL_INSTANCE='housing-equipment-dashboard:asia-east1:he-dashboard-db-80'
export MCI_MYSQL_DATABASE='dashboard_db'
export MCI_MYSQL_USER='app_user'
export MCI_MYSQL_PASSWORD='JAg!5n{?]Jw7'
export MCI_MYSQL_HOST='127.0.0.1'
```

### 6. Cloud SQL Proxyを起動

```bash
./cloud-sql-proxy $CLOUD_SQL_INSTANCE &
```

成功すると以下のメッセージが表示される：
```
The proxy has started successfully and is ready for new connections!
```

### 7. インポート実行

```bash
# 新規データのみインポート（既存はスキップ）
node import_to_db.js

# 全データを再インポート（既存データを削除）
node import_to_db.js --force

# 特定のハウスIDのみインポート
node import_to_db.js --house=2025080001

# 特定のハウスIDを再インポート
node import_to_db.js --house=2025080001 --force
```

**注意**: 95ファイル × 約62万行 = 約5,900万行のインポートには30分〜1時間程度かかります。

## CSV更新時

1. 新しいCSVファイルをCloud Shellにアップロード
2. `node import_to_db.js --force` で再インポート

## インポート完了後のテスト

```bash
curl "https://mock-api-stg-47eqsnsufa-an.a.run.app/0.2/estimated_data?service_provider=9991&house=2025080001&sts=1718294400&ets=1718380800&time_units=20"
```

## トラブルシューティング

### 接続エラー

```
Error: connect ECONNREFUSED 127.0.0.1:3306
```

→ Cloud SQL Proxyが起動しているか確認: `ps aux | grep cloud-sql-proxy`

### 権限エラー

```
Error: Access denied for user
```

→ 環境変数の設定を確認

### メモリ不足

```
FATAL ERROR: Reached heap limit
```

→ メモリを増やして実行: `node --max-old-space-size=4096 import_to_db.js`
