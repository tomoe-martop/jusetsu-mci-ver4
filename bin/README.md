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

以下の環境変数を設定する必要があります：

- `MCI_MYSQL_USER` - MySQLユーザー名
- `MCI_MYSQL_PASSWORD` - MySQLパスワード
- `MCI_MYSQL_HOST` - MySQLホスト（Cloud SQL接続文字列など）
- `MCI_MYSQL_DATABASE` - データベース名
- `MCI_MYSQL_TIMEZONE` - タイムゾーン（デフォルト: Asia/Tokyo）
- `API_SHARED_PASSWORD` - Energy Gateway APIの共有パスワード
- `LOG_LEVEL` - ログレベル（オプション、デフォルト: ERROR）

### 4. Cloud SQLへの接続設定（推奨）

Cloud SQLを使用する場合：

```bash
# Cloud SQL接続名を取得
gcloud sql instances describe INSTANCE_NAME --format="value(connectionName)"

# 接続名の例: project-id:region:instance-name
```

## デプロイ手順

### 方法1: 自動デプロイスクリプトを使用（推奨）

```bash
cd bin
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

```bash
gcloud run jobs execute jusetsu-mci --region asia-northeast1
```

### 定期実行の設定（Cloud Scheduler使用）

```bash
# Cloud Schedulerジョブを作成（毎分実行の例）
gcloud scheduler jobs create http jusetsu-mci-scheduler \
  --location asia-northeast1 \
  --schedule "* * * * *" \
  --uri "https://asia-northeast1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/jusetsu-mci:run" \
  --http-method POST \
  --oauth-service-account-email $PROJECT_NUMBER-compute@developer.gserviceaccount.com

# プロジェクト番号を取得
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
```

## ジョブの監視

### ジョブの実行状況を確認

```bash
gcloud run jobs executions list --job jusetsu-mci --region asia-northeast1
```

### ログの確認

```bash
# 最新のログを表示
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=jusetsu-mci" --limit 50 --format json

# リアルタイムでログを表示
gcloud alpha logging tail "resource.type=cloud_run_job AND resource.labels.job_name=jusetsu-mci"
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

1. ログを確認
```bash
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=jusetsu-mci AND severity>=ERROR" --limit 10
```

2. 環境変数が正しく設定されているか確認
```bash
gcloud run jobs describe jusetsu-mci --region asia-northeast1 --format="value(spec.template.spec.containers[0].env)"
```

3. Cloud SQL接続の確認（Cloud SQL使用時）
```bash
gcloud run jobs describe jusetsu-mci --region asia-northeast1 --format="value(spec.template.spec.containers[0].cloudSqlInstances)"
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
