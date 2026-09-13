#!/bin/bash

# エラー時に終了
set -e

# カラー定義
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Mock API Server Cloud Run デプロイスクリプト ===${NC}\n"

# プロジェクトルートディレクトリ
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${YELLOW}プロジェクトルート: $SCRIPT_DIR${NC}"

# GCPプロジェクトIDを取得
PROJECT_ID=$(gcloud config get-value project)
if [ -z "$PROJECT_ID" ]; then
    echo "エラー: GCPプロジェクトが設定されていません"
    echo "以下のコマンドで設定してください:"
    echo "  gcloud config set project YOUR_PROJECT_ID"
    exit 1
fi

echo -e "${GREEN}✓ プロジェクトID: $PROJECT_ID${NC}"

# 環境変数
SERVICE_NAME="mock-api-stg"
REGION="asia-northeast1"
IMAGE_NAME="gcr.io/$PROJECT_ID/$SERVICE_NAME"

echo -e "${GREEN}✓ サービス名: $SERVICE_NAME${NC}"
echo -e "${GREEN}✓ リージョン: $REGION${NC}"
echo -e "${GREEN}✓ イメージ: $IMAGE_NAME${NC}\n"

# .env.stgから環境変数を読み込み（ビルドに時間をかける前に検証する）
ENV_FILE="$(dirname "$SCRIPT_DIR")/.env.stg"
if [ -f "$ENV_FILE" ]; then
    echo -e "${GREEN}✓ .env.stgを読み込みます${NC}"
    set -a
    source "$ENV_FILE"
    set +a
else
    echo -e "${YELLOW}⚠ .env.stgが見つかりません${NC}"
fi

# Cloud SQL接続設定
CLOUD_SQL_INSTANCE="${CLOUD_SQL_INSTANCE:-}"
if [ -z "$CLOUD_SQL_INSTANCE" ]; then
    echo "エラー: CLOUD_SQL_INSTANCEが設定されていません"
    exit 1
fi

# ステップ1: Cloud Build でイメージをビルド＆プッシュ
# ローカルの docker build は Mac(arm64) から amd64 を作るため --platform 指定が必要で、
# Docker Desktop が不調だとビルドが進まない（2026-09-13 に buildkit が落ちて失敗）。
# ver4 本体の bin/deploy.sh と同じ Cloud Build 方式に寄せる（Cloud Build の既定は linux/amd64）。
# ビルドコンテキストから除外するものは .gcloudignore で管理する（node_modules 等）。
echo -e "${YELLOW}ステップ1: Cloud Build でイメージをビルドしています...${NC}"
gcloud builds submit --tag "$IMAGE_NAME" .

echo -e "${GREEN}✓ イメージのビルドとプッシュが完了しました${NC}\n"

# ステップ2: Cloud Runにデプロイ
echo -e "${YELLOW}ステップ2: Cloud Runにデプロイしています...${NC}"

gcloud run deploy $SERVICE_NAME \
  --image $IMAGE_NAME \
  --region $REGION \
  --platform managed \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 1 \
  --max-instances 10 \
  --min-instances 0 \
  --port 8080 \
  --set-cloudsql-instances "$CLOUD_SQL_INSTANCE" \
  --set-env-vars "USE_DATABASE=true,MCI_MYSQL_HOST=/cloudsql/$CLOUD_SQL_INSTANCE,MCI_MYSQL_USER=$MCI_MYSQL_USER,MCI_MYSQL_PASSWORD=$MCI_MYSQL_PASSWORD,MCI_MYSQL_DATABASE=$MCI_MYSQL_DATABASE"

echo -e "${GREEN}✓ デプロイが完了しました${NC}\n"

# サービスURLを取得
SERVICE_URL=$(gcloud run services describe $SERVICE_NAME --region $REGION --format="value(status.url)")

echo -e "${GREEN}=== デプロイ完了 ===${NC}"
echo "サービス名: $SERVICE_NAME"
echo "リージョン: $REGION"
echo "イメージ: $IMAGE_NAME"
echo "URL: $SERVICE_URL"
echo ""
echo -e "${YELLOW}APIエンドポイント:${NC}"
echo "  $SERVICE_URL/0.2/estimated_data"
echo ""
echo -e "${YELLOW}テストコマンド:${NC}"
echo "  curl \"$SERVICE_URL/0.2/estimated_data?service_provider=9991&house=2025080001&sts=1718336280&ets=1718336400&time_units=20\""