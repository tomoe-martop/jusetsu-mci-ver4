#!/bin/bash

set -e

# 色付き出力
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Cloud Run Jobs デプロイスクリプト ===${NC}\n"

# プロジェクトルートに移動
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo -e "${YELLOW}プロジェクトルート: $PROJECT_ROOT${NC}\n"

# Google Cloud プロジェクトIDの確認
PROJECT_ID=$(gcloud config get-value project 2>/dev/null)

if [ -z "$PROJECT_ID" ]; then
    echo -e "${RED}エラー: Google Cloudプロジェクトが設定されていません${NC}"
    echo "以下のコマンドでプロジェクトを設定してください："
    echo "  gcloud config set project YOUR_PROJECT_ID"
    exit 1
fi

echo -e "${GREEN}✓ プロジェクトID: $PROJECT_ID${NC}"

# 変数設定
# 環境に応じてジョブ名を設定（デフォルト: stg-mci-ver4）
# 本番環境の場合: export ENV=prd を設定してから実行
ENV="${ENV:-stg}"
JOB_NAME="${ENV}-mci-ver4"
REGION="asia-northeast1"
IMAGE_NAME="gcr.io/$PROJECT_ID/$JOB_NAME"

echo -e "${GREEN}✓ 環境: $ENV${NC}"
echo -e "${GREEN}✓ ジョブ名: $JOB_NAME${NC}"
echo -e "${GREEN}✓ リージョン: $REGION${NC}"
echo -e "${GREEN}✓ イメージ: $IMAGE_NAME${NC}\n"

# .envファイルの読み込み
ENV_FILE="$PROJECT_ROOT/.env.$ENV"
if [ -f "$ENV_FILE" ]; then
    echo -e "${GREEN}✓ .env.$ENV ファイルを読み込みます${NC}"
    set -a
    source "$ENV_FILE"
    set +a
else
    echo -e "${YELLOW}⚠ .env.$ENV ファイルが見つかりません。環境変数で設定されているか確認します${NC}"
fi

# オプション環境変数の設定（Cloud SQL確認のため先に取得）
MCI_MYSQL_TIMEZONE="${MCI_MYSQL_TIMEZONE:-Asia/Tokyo}"
LOG_LEVEL="${LOG_LEVEL:-ERROR}"
USE_CLOUD_SQL="${USE_CLOUD_SQL:-false}"

# 環境変数の確認
echo -e "${YELLOW}環境変数の設定を確認しています...${NC}"

# 必須環境変数のリスト
REQUIRED_VARS=(
    "MCI_MYSQL_USER"
    "MCI_MYSQL_PASSWORD"
    "MCI_MYSQL_DATABASE"
    "API_SHARED_PASSWORD"
)

# Cloud SQLを使わない場合のみMCI_MYSQL_HOSTが必須
if [ "$USE_CLOUD_SQL" != "true" ]; then
    REQUIRED_VARS+=("MCI_MYSQL_HOST")
fi

MISSING_VARS=()

for VAR in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!VAR}" ]; then
        MISSING_VARS+=("$VAR")
    fi
done

if [ ${#MISSING_VARS[@]} -gt 0 ]; then
    echo -e "${RED}エラー: 以下の環境変数が設定されていません:${NC}"
    for VAR in "${MISSING_VARS[@]}"; do
        echo "  - $VAR"
    done
    echo ""
    echo "環境変数を設定してから再実行してください："
    echo "  export MCI_MYSQL_USER='your-user'"
    echo "  export MCI_MYSQL_PASSWORD='your-password'"
    if [ "$USE_CLOUD_SQL" != "true" ]; then
        echo "  export MCI_MYSQL_HOST='your-host'"
    fi
    echo "  export MCI_MYSQL_DATABASE='your-database'"
    echo "  export API_SHARED_PASSWORD='your-api-password'"
    exit 1
fi

echo -e "${GREEN}✓ 必須環境変数が設定されています${NC}\n"

# Cloud SQL接続名の確認
CLOUD_SQL_INSTANCE="${CLOUD_SQL_INSTANCE:-}"

if [ "$USE_CLOUD_SQL" = "true" ] && [ -z "$CLOUD_SQL_INSTANCE" ]; then
    echo -e "${RED}エラー: USE_CLOUD_SQL=trueの場合、CLOUD_SQL_INSTANCEを設定してください${NC}"
    echo "例: export CLOUD_SQL_INSTANCE='project-id:region:instance-name'"
    exit 1
fi

# ビルド開始
echo -e "${YELLOW}ステップ1: Dockerイメージをビルドしています...${NC}"
gcloud builds submit --tag "$IMAGE_NAME"

if [ $? -ne 0 ]; then
    echo -e "${RED}エラー: イメージのビルドに失敗しました${NC}"
    exit 1
fi

echo -e "${GREEN}✓ イメージのビルドが完了しました${NC}\n"

# デプロイ
echo -e "${YELLOW}ステップ2: Cloud Run Jobsにデプロイしています...${NC}"

# 環境変数の構築
# Cloud SQL使用時はMCI_MYSQL_HOSTを自動設定
if [ "$USE_CLOUD_SQL" = "true" ]; then
    echo -e "${GREEN}✓ Cloud SQL接続を有効化します: $CLOUD_SQL_INSTANCE${NC}"
    MCI_MYSQL_HOST="/cloudsql/$CLOUD_SQL_INSTANCE"
fi

ENV_VARS="GOOGLE_CLOUD_PROJECT=$PROJECT_ID,MCI_MYSQL_USER=$MCI_MYSQL_USER,MCI_MYSQL_PASSWORD=$MCI_MYSQL_PASSWORD,MCI_MYSQL_HOST=$MCI_MYSQL_HOST,MCI_MYSQL_DATABASE=$MCI_MYSQL_DATABASE,MCI_MYSQL_TIMEZONE=$MCI_MYSQL_TIMEZONE,API_SHARED_PASSWORD=$API_SHARED_PASSWORD,LOG_LEVEL=$LOG_LEVEL"

# GCS_LOG_BUCKETが設定されている場合は追加
if [ -n "$GCS_LOG_BUCKET" ]; then
    ENV_VARS="$ENV_VARS,GCS_LOG_BUCKET=$GCS_LOG_BUCKET"
    echo -e "${GREEN}✓ Cloud Storage ログ保存先: gs://$GCS_LOG_BUCKET/logs/${NC}"
fi

# ENERGY_GATEWAY_API_URLが設定されている場合は追加
if [ -n "$ENERGY_GATEWAY_API_URL" ]; then
    ENV_VARS="$ENV_VARS,ENERGY_GATEWAY_API_URL=$ENERGY_GATEWAY_API_URL"
    echo -e "${GREEN}✓ Energy Gateway API URL: $ENERGY_GATEWAY_API_URL${NC}"
fi

# MOCK_API_URLが設定されている場合は追加（spid=9991の場合のみ使用）
echo -e "${YELLOW}DEBUG: MOCK_API_URL=$MOCK_API_URL${NC}"
if [ -n "$MOCK_API_URL" ]; then
    ENV_VARS="$ENV_VARS,MOCK_API_URL=$MOCK_API_URL"
    echo -e "${GREEN}✓ Mock API URL (spid=9991用): $MOCK_API_URL${NC}"
fi

# デプロイコマンドの構築
DEPLOY_CMD="gcloud run jobs deploy $JOB_NAME \
  --image $IMAGE_NAME \
  --region $REGION \
  --parallelism 1 \
  --set-env-vars \"$ENV_VARS\""

# Cloud SQL使用時は接続オプションを追加
if [ "$USE_CLOUD_SQL" = "true" ]; then
    DEPLOY_CMD="$DEPLOY_CMD \
  --set-cloudsql-instances \"$CLOUD_SQL_INSTANCE\""
fi

# デプロイ実行
eval $DEPLOY_CMD

if [ $? -ne 0 ]; then
    echo -e "${RED}エラー: デプロイに失敗しました${NC}"
    exit 1
fi

echo -e "${GREEN}✓ デプロイが完了しました${NC}\n"

# デプロイ情報の表示
echo -e "${GREEN}=== デプロイ完了 ===${NC}"
echo -e "ジョブ名: $JOB_NAME"
echo -e "リージョン: $REGION"
echo -e "イメージ: $IMAGE_NAME"
echo ""
echo -e "${YELLOW}ジョブを手動で実行する場合:${NC}"
echo -e "  gcloud run jobs execute $JOB_NAME --region $REGION"
echo ""
echo -e "${YELLOW}ログを確認する場合:${NC}"
echo -e "  gcloud logging read \"resource.type=cloud_run_job AND resource.labels.job_name=$JOB_NAME\" --limit 50"
echo ""
echo -e "${YELLOW}ジョブの詳細を確認する場合:${NC}"
echo -e "  gcloud run jobs describe $JOB_NAME --region $REGION"
echo ""
