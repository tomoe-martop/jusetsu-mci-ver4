# Cloud Run Jobs デプロイ手順

このドキュメントでは、jusetsu-mci-ver4をGoogle Cloud Run Jobsにデプロイする手順を説明します。

## 前提条件

- Google Cloud SDKがインストールされていること
- Google Cloudプロジェクトが作成されていること
- 必要なAPIが有効化されていること
  - Cloud Run API
  - Cloud Build API
  - Artifact Registry API

## 初回セットアップ

### 1. Google Cloudプロジェクトの設定

```bash
# プロジェクトIDを設定
export PROJECT_ID="housing-equipment-dashboard"

# gcloudの設定
gcloud config set project $PROJECT_ID
gcloud auth login
```

### 2. 必要なAPIを有効化

```bash
gcloud services enable run.googleapis.com
gcloud services enable cloudbuild.googleapis.com
gcloud services enable artifactregistry.googleapis.com
```

### 3. 環境変数の準備

環境変数は`.env`ファイルで管理することを推奨します。

**ステージング環境の設定**:
```bash
# テンプレートをコピー
cp .env.stg.example .env.stg

# エディタで .env.stg を編集して実際の値を設定
```

**本番環境の設定**:
```bash
# テンプレートをコピー
cp .env.prd.example .env.prd

# エディタで .env.prd を編集して実際の値を設定
```

**必要な環境変数**:
- `MCI_MYSQL_USER` - MySQLユーザー名
- `MCI_MYSQL_PASSWORD` - MySQLパスワード
- `MCI_MYSQL_HOST` - MySQLホスト（Cloud SQL接続文字列など）
- `MCI_MYSQL_DATABASE` - データベース名
- `MCI_MYSQL_TIMEZONE` - タイムゾーン（デフォルト: Asia/Tokyo）
- `API_SHARED_PASSWORD` - Energy Gateway APIの共有パスワード
- `LOG_LEVEL` - 標準出力（Cloud Logging）のログレベル（オプション、デフォルト: INFO。EGPF の再送試行ログは INFO のため ERROR にすると Cloud Logging に残らない。`predictor.log`／GCS 退避ログには LOG_LEVEL に関わらず INFO 以上が常に記録される）
- `GCS_LOG_BUCKET` - Cloud Storageのログ保存先バケット名（オプション、設定しない場合はローカル保存）
- `ENERGY_GATEWAY_API_URL` - Energy Gateway APIのURL（オプション、デフォルト: https://api.energy-gateway.jp/0.2/estimated_data）
- `MOCK_API_URL` - モックAPIのURL（オプション、spid=9991の場合のみ使用）
- `ERROR_NOTIFY_SLACK_WEBHOOK_URL` - エラー通知先の Slack Incoming Webhook URL（オプション。未設定なら通知しない）
- `ERROR_NOTIFY_ENV_LABEL` - 通知の先頭に付く環境ラベル（オプション。既定: `ENV=prd` なら `本番`、それ以外は `STG`）
- `EGPF_RETRY_MAX_ATTEMPTS` / `EGPF_RETRY_WAIT_SEC` / `EGPF_CONNECT_TIMEOUT_SEC` / `EGPF_READ_TIMEOUT_SEC` / `EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES` - EGPF リトライ設定（オプション。設定した変数だけデプロイに載る。既定: 5回 / 2秒 / 10秒 / 30秒 / 3）

**注意**: `--set-env-vars` はカンマ区切りのため、各値にカンマを含めないでください。

**自動設定される環境変数**（deploy.shが自動的に設定）:
- `GOOGLE_CLOUD_PROJECT` - GCPプロジェクトID（多重実行防止のチェックに使用）

**注意**: `.env.stg`と`.env.prd`ファイルは機密情報を含むため、Gitにコミットしないでください（`.gitignore`で除外済み）

### 4. Cloud SQLへの接続設定

Cloud SQLを使用する場合、以下の手順で接続情報を取得してください：

```bash
# Cloud SQL接続名を取得
gcloud sql instances describe INSTANCE_NAME --format="value(connectionName)"

# 接続名の例: your-project-id:asia-northeast1:your-instance-name
```

取得した接続名を`.env.stg`または`.env.prd`ファイルに設定：

```bash
# .env.stg または .env.prd に以下を設定
USE_CLOUD_SQL=true
CLOUD_SQL_INSTANCE=your-project-id:asia-northeast1:your-instance-name

# 注意: MCI_MYSQL_HOSTは設定不要です（deploy.shが自動設定します）
```

**Cloud SQLユーザーの権限設定**:

アプリケーションが必要とするテーブル操作を行うために、MySQLユーザーに適切な権限を付与してください：

```bash
# Cloud SQLに接続
gcloud sql connect INSTANCE_NAME --user=root --database=dashboard_db
```

MySQLコンソールで以下を実行：

```sql
-- Cloud SQL Proxy経由のユーザーを作成（まだ作成していない場合）
CREATE USER 'app_user'@'cloudsqlproxy~%' IDENTIFIED BY 'your-password';

-- 必要な権限を付与
GRANT SELECT ON dashboard_db.* TO 'app_user'@'cloudsqlproxy~%';
GRANT UPDATE ON dashboard_db.tasks TO 'app_user'@'cloudsqlproxy~%';
GRANT UPDATE ON dashboard_db.task_houses TO 'app_user'@'cloudsqlproxy~%';
GRANT INSERT ON dashboard_db.task_results TO 'app_user'@'cloudsqlproxy~%';
FLUSH PRIVILEGES;
```

**注意**: 本番環境（prd）でも同様の権限設定が必要です。

### 5. Cloud Storageへのログ・データ保存設定（推奨）

ログファイルとCSVファイルをCloud Storageに保存する場合、以下の手順でバケットを作成してください：

```bash
# ログ・データ保存用バケットを作成
gsutil mb -l asia-northeast1 gs://your-log-bucket-name

# Cloud Run Jobs用のサービスアカウントに書き込み権限を付与
gsutil iam ch serviceAccount:YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com:objectCreator gs://your-log-bucket-name
```

`.env.stg`または`.env.prd`ファイルにバケット名を設定：

```bash
# .env.stg または .env.prd に以下を設定
GCS_LOG_BUCKET=your-log-bucket-name
```

**バケット内のフォルダ構成**:
- `logs/`: predictor.logファイル（実行ログ）
- `data/`: 入力CSVファイル（電力データ）

**注意**: `GCS_LOG_BUCKET`を設定しない場合、ファイルはコンテナ内のローカルフォルダに保存されますが、Cloud Run Jobsの終了時に削除されます。

## デプロイ手順

### 方法1: 自動デプロイスクリプトを使用（推奨）

**ステージング環境へのデプロイ** (ジョブ名: `stg-mci-ver4`):
```bash
cd bin
./deploy.sh
# ENVを指定しない場合、デフォルトでstgになります
```

**本番環境へのデプロイ** (ジョブ名: `prd-mci-ver4`):
```bash
cd bin
export ENV=prd
./deploy.sh
# 本番環境は必ずENV=prdを指定してください
```

### 方法2: 手動デプロイ

#### ステップ1: Dockerイメージをビルドしてプッシュ

```bash
# プロジェクトルートに移動
cd /path/to/jusetsu-mci-ver4

# イメージをビルドしてContainer Registryにプッシュ
gcloud builds submit --tag gcr.io/$PROJECT_ID/jusetsu-mci
```

#### ステップ2: Cloud Run Jobsにデプロイ

```bash
# 基本的なデプロイ（Cloud SQL使用しない場合）
gcloud run jobs deploy jusetsu-mci \
  --image gcr.io/$PROJECT_ID/jusetsu-mci \
  --region asia-northeast1 \
  --max-instances 1 \
  --set-env-vars "MCI_MYSQL_USER=xxx,MCI_MYSQL_PASSWORD=xxx,MCI_MYSQL_HOST=xxx,MCI_MYSQL_DATABASE=xxx,MCI_MYSQL_TIMEZONE=Asia/Tokyo,API_SHARED_PASSWORD=xxx,LOG_LEVEL=INFO"

# Cloud SQL使用する場合
gcloud run jobs deploy jusetsu-mci \
  --image gcr.io/$PROJECT_ID/jusetsu-mci \
  --region asia-northeast1 \
  --max-instances 1 \
  --set-env-vars "MCI_MYSQL_USER=xxx,MCI_MYSQL_PASSWORD=xxx,MCI_MYSQL_DATABASE=xxx,MCI_MYSQL_TIMEZONE=Asia/Tokyo,API_SHARED_PASSWORD=xxx,LOG_LEVEL=INFO" \
  --set-cloudsql-instances "project-id:region:instance-name" \
  --update-env-vars "MCI_MYSQL_HOST=/cloudsql/project-id:region:instance-name"
```

## ジョブの実行

### 手動実行

**ステージング環境**:
```bash
gcloud run jobs execute stg-mci-ver4 --region asia-northeast1
```

**本番環境**:
```bash
gcloud run jobs execute prd-mci-ver4 --region asia-northeast1
```

### 定期実行の設定（Cloud Scheduler使用）

**ステージング環境** (毎分実行の例):
```bash
# プロジェクト番号を取得
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")

# Cloud Schedulerジョブを作成
gcloud scheduler jobs create http stg-mci-ver4-scheduler \
  --location asia-northeast1 \
  --schedule "* * * * *" \
  --uri "https://asia-northeast1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/stg-mci-ver4:run" \
  --http-method POST \
  --oauth-service-account-email $PROJECT_NUMBER-compute@developer.gserviceaccount.com
```

**本番環境** (毎分実行の例):
```bash
# プロジェクト番号を取得
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")

# Cloud Schedulerジョブを作成
gcloud scheduler jobs create http prd-mci-ver4-scheduler \
  --location asia-northeast1 \
  --schedule "* * * * *" \
  --uri "https://asia-northeast1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/prd-mci-ver4:run" \
  --http-method POST \
  --oauth-service-account-email $PROJECT_NUMBER-compute@developer.gserviceaccount.com
```

## 事前取得ジョブ（EG_DASHBOARD-102）

EGPF 1分値を毎日 05:00（JST）に事前取得してキャッシュする `prefetch.py` のジョブです（`<ENV>-mci-egpf-prefetch`）。予測ジョブと同じイメージ（`gcr.io/$PROJECT_ID/<ENV>-mci-ver4`）を使います。仕様は `README.md` の「EGPF 1分値の事前取得ジョブ」と設計書 3.3 を参照してください。

### デプロイ対象の指定（`DEPLOY_TARGET`）

| `DEPLOY_TARGET` | 動作 |
|---|---|
| `main`（既定） | 従来どおり。イメージをビルドして予測ジョブ `<ENV>-mci-ver4` をデプロイ |
| `prefetch` | 事前取得ジョブ `<ENV>-mci-egpf-prefetch` のみデプロイ。**ビルドしない**（既存イメージに `prefetch.py` が入っている前提。deploy.sh も警告を出す） |
| `all` | イメージをビルドして両方のジョブをデプロイ |

```bash
ENV=stg bin/deploy.sh                          # 予測ジョブ（従来どおり）
ENV=stg DEPLOY_TARGET=prefetch bin/deploy.sh   # 事前取得ジョブのみ（既存イメージ）
ENV=stg DEPLOY_TARGET=all bin/deploy.sh        # ビルドして両方
```

- **初回・コード更新時は `DEPLOY_TARGET=all`**（または先に `main` でビルドしてから `prefetch`）。`prefetch` 単独は、既存イメージ（`:latest`）に `prefetch.py` が入っていないと毎回 `can't open file` で失敗します
- 予測ジョブと同じイメージ・タグを共用するため、**両ジョブのコードの版は連動します**。別ブランチ（release 系など）から `main` でビルドすると、事前取得ジョブも次の実行からそのコードで動きます。`prefetch` 単独のデプロイはジョブ設定（環境変数・VPC 等）だけを変えたいときに使います

事前取得ジョブは次の設定でデプロイされます: `--command python --args prefetch.py`、`--max-retries 0`、`--parallelism 1`、`--task-timeout $PREFETCH_TASK_TIMEOUT`（既定 `14400s`）。

### 事前取得ジョブ用の設定（`.env.stg` / `.env.prd`）

| 変数 | 必須 | 内容 |
|---|---|---|
| `EGPF_CACHE_BUCKET` | ○ | キャッシュ用バケット名（例: `stg-mci-egpf-cache`）。未設定だと deploy.sh がエラーで止まる。`<ENV>-` で始まらない名前もエラー（予測ジョブのデプロイでも同じ判定。STG・本番は同じ GCP プロジェクトのため、シェルに残った別環境の値での取り違えを防ぐ） |
| `JOB_SERVICE_ACCOUNT` | 推奨 | `--service-account`。既存の予測ジョブと同じ SA を指定する。未設定なら警告のみ（既定の compute SA になり、既存ジョブと同じ） |
| `VPC_NETWORK` / `VPC_SUBNET` / `VPC_EGRESS` | ○ | `--network` / `--subnet` / `--vpc-egress`。既存の予測ジョブと同じ Direct VPC egress を指定する（例: `default` / `default` / `private-ranges-only`）。1 つでも未設定だと deploy.sh がビルド前にエラーで止まる（VPC なしだと DB に内部 IP で接続できない） |
| `PREFETCH_TASK_TIMEOUT` | | タスクのタイムアウト（既定 `14400s`） |
| `EGPF_PREFETCH_INTERVAL_SEC` / `EGPF_PREFETCH_CATCHUP_DAYS` / `EGPF_PREFETCH_MAX_REQUESTS` / `EGPF_PREFETCH_MAX_MINUTES` | | 設定したものだけジョブに載る（未設定ならコードの既定 1 / 7 / 4000 / 100） |
| `EGPF_PREFETCH_BASE_DATE` | ×（設定しない） | 試験用の基準日。**ジョブ定義には載せない**。`ENV=prd` で設定されているとエラーで止まり、それ以外は警告を出して載せずにデプロイする。試験時は下記の `execute --update-env-vars` で実行ごとに指定する |

- `JOB_SERVICE_ACCOUNT`・`VPC_*` は事前取得ジョブにだけ付けます（予測ジョブのコマンドは従来どおり）
- ジョブの環境変数は、DB・API パスワード（予測ジョブと同じ）、`LOG_LEVEL`、`EGPF_CACHE_BUCKET`、設定されていれば `ENERGY_GATEWAY_API_URL`・`MOCK_API_URL`・`EGPF_RETRY_*`・`EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES`・`EGPF_PREFETCH_*` です（`EGPF_CACHE_ENABLED` は予測時のフラグなので載せません）

試験で基準日だけ変えて手動実行する場合（その実行だけ上書き。ジョブ定義は変わらないので、翌朝の定期実行は実際の日付で動く）:

```bash
gcloud run jobs execute stg-mci-egpf-prefetch --region asia-northeast1 --update-env-vars EGPF_PREFETCH_BASE_DATE=2026-10-05
```

`.env.stg` に `EGPF_PREFETCH_BASE_DATE` を書いたり、シェルで export したまま deploy.sh を実行しないでください（ジョブ定義に載ると毎日同じ日を見て「保存済み」でスキップし続け、新しい日を取得しないまま正常終了します。deploy.sh は載せませんが、警告が出たら `unset` してください）。

### 定期実行（Cloud Scheduler、毎日 05:00 JST）

deploy.sh では作りません。既存の MCI 用 Scheduler は UTC のため、`--time-zone Asia/Tokyo` を必ず指定してください。

**ステージング環境**:
```bash
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")

gcloud scheduler jobs create http stg-mci-egpf-prefetch-scheduler \
  --location asia-northeast1 \
  --schedule "0 5 * * *" \
  --time-zone "Asia/Tokyo" \
  --uri "https://asia-northeast1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/stg-mci-egpf-prefetch:run" \
  --http-method POST \
  --oauth-service-account-email $PROJECT_NUMBER-compute@developer.gserviceaccount.com
```

**本番環境**:
```bash
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")

gcloud scheduler jobs create http prd-mci-egpf-prefetch-scheduler \
  --location asia-northeast1 \
  --schedule "0 5 * * *" \
  --time-zone "Asia/Tokyo" \
  --uri "https://asia-northeast1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/prd-mci-egpf-prefetch:run" \
  --http-method POST \
  --oauth-service-account-email $PROJECT_NUMBER-compute@developer.gserviceaccount.com
```

確認: `gcloud scheduler jobs describe stg-mci-egpf-prefetch-scheduler --location asia-northeast1 --format="value(schedule,timeZone,state)"` → `0 5 * * *  Asia/Tokyo  ENABLED`

### ログの確認

```bash
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=stg-mci-egpf-prefetch AND textPayload:\"Prefetch summary\"" --limit 10
```

## ジョブの監視

### ジョブの実行状況を確認

**ステージング環境**:
```bash
gcloud run jobs executions list --job stg-mci-ver4 --region asia-northeast1
```

**本番環境**:
```bash
gcloud run jobs executions list --job prd-mci-ver4 --region asia-northeast1
```

### ログの確認

**ステージング環境**:
```bash
# 最新のログを表示
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=stg-mci-ver4" --limit 50 --format json

# リアルタイムでログを表示
gcloud logging tail "resource.type=cloud_run_job AND resource.labels.job_name=stg-mci-ver4"
```

**本番環境**:
```bash
# 最新のログを表示
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=prd-mci-ver4" --limit 50 --format json

# リアルタイムでログを表示
gcloud logging tail "resource.type=cloud_run_job AND resource.labels.job_name=prd-mci-ver4"
```

## 更新

アプリケーションを更新する場合：

```bash
# 1. コードを修正
# 2. 再デプロイ
cd bin
./deploy.sh
```

## トラブルシューティング

### ジョブが失敗する場合

1. ログを確認（例: ステージング環境）
```bash
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=stg-mci-ver4 AND severity>=ERROR" --limit 10
```

2. 環境変数が正しく設定されているか確認（例: ステージング環境）
```bash
gcloud run jobs describe stg-mci-ver4 --region asia-northeast1 --format="value(spec.template.spec.containers[0].env)"
```

3. Cloud SQL接続の確認（Cloud SQL使用時、例: ステージング環境）
```bash
gcloud run jobs describe stg-mci-ver4 --region asia-northeast1 --format="value(spec.template.spec.containers[0].cloudSqlInstances)"
```

### 二重起動の防止

Cloud Run Jobsは`--max-instances 1`を設定することで、同時に1つのインスタンスのみが実行されます。
さらに、アプリケーション側でも`tasks`テーブルの`start_at`フィールドを使用して二重処理を防止しています。

## セキュリティのベストプラクティス

### Secret Managerの使用（推奨）

機密情報は環境変数ではなくSecret Managerを使用することを推奨します：

```bash
# シークレットを作成
echo -n "your-password" | gcloud secrets create mci-mysql-password --data-file=-

# Cloud Run Jobsでシークレットを使用
gcloud run jobs deploy jusetsu-mci \
  --image gcr.io/$PROJECT_ID/jusetsu-mci \
  --region asia-northeast1 \
  --max-instances 1 \
  --set-secrets "MCI_MYSQL_PASSWORD=mci-mysql-password:latest"
```

## コスト最適化

- `--max-instances 1`: 同時実行を制限
- `--memory 512Mi`: 必要に応じてメモリを調整
- `--cpu 1`: 必要に応じてCPUを調整
- タイムアウトの設定: `--task-timeout 3600s`（デフォルトは10分）

## 参考リンク

- [Cloud Run Jobs ドキュメント](https://cloud.google.com/run/docs/create-jobs)
- [Cloud Scheduler ドキュメント](https://cloud.google.com/scheduler/docs)
- [Cloud SQL 接続](https://cloud.google.com/sql/docs/mysql/connect-run)
