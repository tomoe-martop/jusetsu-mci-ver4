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
export PROJECT_ID="your-project-id"

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
- `LOG_LEVEL` - ログレベル（オプション、デフォルト: ERROR）
- `GCS_LOG_BUCKET` - Cloud Storageのログ保存先バケット名（オプション、設定しない場合はローカル保存）

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
# または明示的に
export ENV=stg
./deploy.sh
```

**本番環境へのデプロイ** (ジョブ名: `prd-mci-ver4`):
```bash
cd bin
export ENV=prd
./deploy.sh
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
  --set-env-vars "MCI_MYSQL_USER=xxx,MCI_MYSQL_PASSWORD=xxx,MCI_MYSQL_HOST=xxx,MCI_MYSQL_DATABASE=xxx,MCI_MYSQL_TIMEZONE=Asia/Tokyo,API_SHARED_PASSWORD=xxx,LOG_LEVEL=ERROR"

# Cloud SQL使用する場合
gcloud run jobs deploy jusetsu-mci \
  --image gcr.io/$PROJECT_ID/jusetsu-mci \
  --region asia-northeast1 \
  --max-instances 1 \
  --set-env-vars "MCI_MYSQL_USER=xxx,MCI_MYSQL_PASSWORD=xxx,MCI_MYSQL_DATABASE=xxx,MCI_MYSQL_TIMEZONE=Asia/Tokyo,API_SHARED_PASSWORD=xxx,LOG_LEVEL=ERROR" \
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
