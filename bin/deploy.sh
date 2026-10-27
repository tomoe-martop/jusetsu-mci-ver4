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
# EGPF 1分値の事前取得ジョブ（EG_DASHBOARD-102）。予測ジョブと同じイメージを使う
PREFETCH_JOB_NAME="${ENV}-mci-egpf-prefetch"
REGION="asia-northeast1"
# イメージ名はジョブ名と切り離して固定（予測ジョブ・事前取得ジョブで共用）
IMAGE_NAME="gcr.io/$PROJECT_ID/${ENV}-mci-ver4"

# デプロイ対象（既定 main = 従来どおり予測ジョブのみ）
#   main     : イメージをビルドして予測ジョブ（$JOB_NAME）をデプロイ
#   prefetch : 事前取得ジョブ（$PREFETCH_JOB_NAME）のみデプロイ。ビルドせず既存イメージを使う
#   all      : イメージをビルドして両方のジョブをデプロイ
DEPLOY_TARGET="${DEPLOY_TARGET:-main}"
case "$DEPLOY_TARGET" in
    main|prefetch|all) ;;
    *)
        echo -e "${RED}エラー: DEPLOY_TARGET は main / prefetch / all のいずれかを指定してください（指定値: $DEPLOY_TARGET）${NC}"
        exit 1
        ;;
esac
DEPLOY_MAIN=false
DEPLOY_PREFETCH=false
BUILD_IMAGE=false
if [ "$DEPLOY_TARGET" = "main" ] || [ "$DEPLOY_TARGET" = "all" ]; then
    DEPLOY_MAIN=true
    BUILD_IMAGE=true
fi
if [ "$DEPLOY_TARGET" = "prefetch" ] || [ "$DEPLOY_TARGET" = "all" ]; then
    DEPLOY_PREFETCH=true
fi

echo -e "${GREEN}✓ 環境: $ENV${NC}"
echo -e "${GREEN}✓ デプロイ対象: $DEPLOY_TARGET${NC}"
echo -e "${GREEN}✓ ジョブ名: $JOB_NAME${NC}"
if [ "$DEPLOY_PREFETCH" = "true" ]; then
    echo -e "${GREEN}✓ 事前取得ジョブ名: $PREFETCH_JOB_NAME${NC}"
fi
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
# LOG_LEVEL は標準出力（Cloud Logging）のレベル。EGPF の再送試行ログは INFO のため既定は INFO
# （predictor.log／GCS 退避ログには LOG_LEVEL に関わらず INFO 以上が常に記録される）
LOG_LEVEL="${LOG_LEVEL:-INFO}"
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

# 事前取得ジョブは EGPF_CACHE_BUCKET が必須（未設定だとジョブが何もせず終了コード 1 になる）
if [ "$DEPLOY_PREFETCH" = "true" ] && [ -z "$EGPF_CACHE_BUCKET" ]; then
    echo -e "${RED}エラー: 事前取得ジョブには EGPF_CACHE_BUCKET の設定が必要です${NC}"
    exit 1
fi

# Cloud SQL接続名の確認
CLOUD_SQL_INSTANCE="${CLOUD_SQL_INSTANCE:-}"

if [ "$USE_CLOUD_SQL" = "true" ] && [ -z "$CLOUD_SQL_INSTANCE" ]; then
    echo -e "${RED}エラー: USE_CLOUD_SQL=trueの場合、CLOUD_SQL_INSTANCEを設定してください${NC}"
    echo "例: export CLOUD_SQL_INSTANCE='project-id:region:instance-name'"
    exit 1
fi

# ビルド開始
if [ "$BUILD_IMAGE" = "true" ]; then
echo -e "${YELLOW}ステップ1: Dockerイメージをビルドしています...${NC}"
gcloud builds submit --tag "$IMAGE_NAME"

if [ $? -ne 0 ]; then
    echo -e "${RED}エラー: イメージのビルドに失敗しました${NC}"
    exit 1
fi

echo -e "${GREEN}✓ イメージのビルドが完了しました${NC}\n"
else
    echo -e "${YELLOW}ステップ1: ビルドは行いません（既存イメージ $IMAGE_NAME を使います）${NC}\n"
fi

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

# エラー通知（Slack Incoming Webhook）。ERROR_NOTIFY_SLACK_WEBHOOK_URL 未設定なら通知は行われない（従来どおりの動作）
# ERROR_NOTIFY_ENV_LABEL は通知の先頭に付く環境ラベル（既定: ENV=prd なら「本番」、それ以外は「STG」）
# ※ --set-env-vars はカンマ区切りのため、値にカンマを含めないこと
if [ "$ENV" = "prd" ]; then
    ERROR_NOTIFY_ENV_LABEL="${ERROR_NOTIFY_ENV_LABEL:-本番}"
else
    ERROR_NOTIFY_ENV_LABEL="${ERROR_NOTIFY_ENV_LABEL:-STG}"
fi
ENV_VARS="$ENV_VARS,ERROR_NOTIFY_ENV_LABEL=$ERROR_NOTIFY_ENV_LABEL"
if [ -n "$ERROR_NOTIFY_SLACK_WEBHOOK_URL" ]; then
    ENV_VARS="$ENV_VARS,ERROR_NOTIFY_SLACK_WEBHOOK_URL=$ERROR_NOTIFY_SLACK_WEBHOOK_URL"
    echo -e "${GREEN}✓ エラー通知: Slack Webhook 設定あり (ラベル: $ERROR_NOTIFY_ENV_LABEL)${NC}"
else
    echo -e "${YELLOW}⚠ ERROR_NOTIFY_SLACK_WEBHOOK_URL が未設定のため、エラー通知は無効です${NC}"
fi

# EGPF リトライ設定（設定されている変数だけ載せる。未設定時は main.py 側の既定値: 5回 / 2秒 / 接続10秒 / 読取30秒 / 連続3回で打ち切り）
for VAR in EGPF_RETRY_MAX_ATTEMPTS EGPF_RETRY_WAIT_SEC EGPF_CONNECT_TIMEOUT_SEC EGPF_READ_TIMEOUT_SEC EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES; do
    if [ -n "${!VAR}" ]; then
        ENV_VARS="$ENV_VARS,$VAR=${!VAR}"
        echo -e "${GREEN}✓ $VAR=${!VAR}${NC}"
    fi
done

# EGPF 1分値キャッシュ（EG_DASHBOARD-102。設定されている変数だけ載せる。EGPF_CACHE_BUCKET 未設定ならキャッシュを使わず従来どおり）
for VAR in EGPF_CACHE_ENABLED EGPF_CACHE_BUCKET; do
    if [ -n "${!VAR}" ]; then
        ENV_VARS="$ENV_VARS,$VAR=${!VAR}"
        echo -e "${GREEN}✓ $VAR=${!VAR}${NC}"
    fi
done

# 事前取得ジョブ（EG_DASHBOARD-102）の環境変数。予測ジョブと共通の DB・API 設定に、キャッシュ・事前取得の設定を足す
# （GCS_LOG_BUCKET・ERROR_NOTIFY_* は事前取得ジョブでは使わないので載せない。EGPF_CACHE_ENABLED は予測時のフラグなので載せない）
PREFETCH_ENV_VARS="GOOGLE_CLOUD_PROJECT=$PROJECT_ID,MCI_MYSQL_USER=$MCI_MYSQL_USER,MCI_MYSQL_PASSWORD=$MCI_MYSQL_PASSWORD,MCI_MYSQL_HOST=$MCI_MYSQL_HOST,MCI_MYSQL_DATABASE=$MCI_MYSQL_DATABASE,MCI_MYSQL_TIMEZONE=$MCI_MYSQL_TIMEZONE,API_SHARED_PASSWORD=$API_SHARED_PASSWORD,LOG_LEVEL=$LOG_LEVEL,EGPF_CACHE_BUCKET=$EGPF_CACHE_BUCKET"
for VAR in ENERGY_GATEWAY_API_URL MOCK_API_URL EGPF_RETRY_MAX_ATTEMPTS EGPF_RETRY_WAIT_SEC EGPF_CONNECT_TIMEOUT_SEC EGPF_READ_TIMEOUT_SEC EGPF_ABORT_AFTER_CONSECUTIVE_FAILURES EGPF_PREFETCH_INTERVAL_SEC EGPF_PREFETCH_CATCHUP_DAYS EGPF_PREFETCH_MAX_REQUESTS EGPF_PREFETCH_BASE_DATE; do
    if [ -n "${!VAR}" ]; then
        PREFETCH_ENV_VARS="$PREFETCH_ENV_VARS,$VAR=${!VAR}"
    fi
done

# デプロイコマンドの構築
# タイムアウト: 24時間（デフォルト10分、最大24時間）
# 計算根拠: 30秒 × 2000件（ハウス） = 60,000秒 = 約16時間40分 + 余裕
# ※これ以上実行ハウスが増える場合は並列処理を検討
TASK_TIMEOUT="${TASK_TIMEOUT:-86400s}"

DEPLOY_CMD="gcloud run jobs deploy $JOB_NAME \
  --image $IMAGE_NAME \
  --region $REGION \
  --parallelism 1 \
  --task-timeout $TASK_TIMEOUT \
  --set-env-vars \"$ENV_VARS\""

# Cloud SQL使用時は接続オプションを追加
if [ "$USE_CLOUD_SQL" = "true" ]; then
    DEPLOY_CMD="$DEPLOY_CMD \
  --set-cloudsql-instances \"$CLOUD_SQL_INSTANCE\""
fi

# 事前取得ジョブのデプロイコマンド（設計書 3.3: 自動リトライ 0 回・既存ジョブと同じ VPC egress・SA を明示指定）
# タイムアウト既定 4 時間（上限 4,000 リクエスト × (待ち 1 秒 + 応答) ≒ 1.3〜1.7 時間 + 余裕）
PREFETCH_TASK_TIMEOUT="${PREFETCH_TASK_TIMEOUT:-14400s}"

PREFETCH_DEPLOY_CMD="gcloud run jobs deploy $PREFETCH_JOB_NAME \
  --image $IMAGE_NAME \
  --region $REGION \
  --command python \
  --args prefetch.py \
  --max-retries 0 \
  --parallelism 1 \
  --task-timeout $PREFETCH_TASK_TIMEOUT \
  --set-env-vars \"$PREFETCH_ENV_VARS\""

if [ "$USE_CLOUD_SQL" = "true" ]; then
    PREFETCH_DEPLOY_CMD="$PREFETCH_DEPLOY_CMD \
  --set-cloudsql-instances \"$CLOUD_SQL_INSTANCE\""
fi

# サービスアカウント・VPC は設定されているものだけ付ける（未設定の項目は警告）
if [ "$DEPLOY_PREFETCH" = "true" ]; then
    if [ -n "$JOB_SERVICE_ACCOUNT" ]; then
        PREFETCH_DEPLOY_CMD="$PREFETCH_DEPLOY_CMD \
  --service-account \"$JOB_SERVICE_ACCOUNT\""
    else
        echo -e "${YELLOW}⚠ JOB_SERVICE_ACCOUNT が未設定です。事前取得ジョブには既存ジョブと同じサービスアカウントを指定してください${NC}"
    fi
    if [ -n "$VPC_NETWORK" ]; then
        PREFETCH_DEPLOY_CMD="$PREFETCH_DEPLOY_CMD \
  --network \"$VPC_NETWORK\""
    else
        echo -e "${YELLOW}⚠ VPC_NETWORK が未設定です。事前取得ジョブには既存ジョブと同じ VPC を指定してください${NC}"
    fi
    if [ -n "$VPC_SUBNET" ]; then
        PREFETCH_DEPLOY_CMD="$PREFETCH_DEPLOY_CMD \
  --subnet \"$VPC_SUBNET\""
    else
        echo -e "${YELLOW}⚠ VPC_SUBNET が未設定です。事前取得ジョブには既存ジョブと同じサブネットを指定してください${NC}"
    fi
    if [ -n "$VPC_EGRESS" ]; then
        PREFETCH_DEPLOY_CMD="$PREFETCH_DEPLOY_CMD \
  --vpc-egress \"$VPC_EGRESS\""
    else
        echo -e "${YELLOW}⚠ VPC_EGRESS が未設定です。事前取得ジョブには既存ジョブと同じ VPC egress を指定してください${NC}"
    fi
fi

# デプロイ実行
DEPLOYED_JOBS=()
if [ "$DEPLOY_MAIN" = "true" ]; then
eval $DEPLOY_CMD

if [ $? -ne 0 ]; then
    echo -e "${RED}エラー: デプロイに失敗しました${NC}"
    exit 1
fi
DEPLOYED_JOBS+=("$JOB_NAME")
fi

if [ "$DEPLOY_PREFETCH" = "true" ]; then
    echo -e "${YELLOW}事前取得ジョブをデプロイしています: $PREFETCH_JOB_NAME${NC}"
    eval $PREFETCH_DEPLOY_CMD

    if [ $? -ne 0 ]; then
        echo -e "${RED}エラー: 事前取得ジョブのデプロイに失敗しました${NC}"
        exit 1
    fi
    DEPLOYED_JOBS+=("$PREFETCH_JOB_NAME")
fi

echo -e "${GREEN}✓ デプロイが完了しました${NC}\n"

# デプロイ情報の表示
for DEPLOYED_JOB in "${DEPLOYED_JOBS[@]}"; do
echo -e "${GREEN}=== デプロイ完了 ===${NC}"
echo -e "ジョブ名: $DEPLOYED_JOB"
echo -e "リージョン: $REGION"
echo -e "イメージ: $IMAGE_NAME"
echo ""
echo -e "${YELLOW}ジョブを手動で実行する場合:${NC}"
echo -e "  gcloud run jobs execute $DEPLOYED_JOB --region $REGION"
echo ""
echo -e "${YELLOW}ログを確認する場合:${NC}"
echo -e "  gcloud logging read \"resource.type=cloud_run_job AND resource.labels.job_name=$DEPLOYED_JOB\" --limit 50"
echo ""
echo -e "${YELLOW}ジョブの詳細を確認する場合:${NC}"
echo -e "  gcloud run jobs describe $DEPLOYED_JOB --region $REGION"
echo ""
done
if [ "$DEPLOY_PREFETCH" = "true" ]; then
    echo -e "${YELLOW}事前取得ジョブの定期実行（Cloud Scheduler、毎日 05:00 JST）は bin/README.md の手順で作成してください${NC}"
    echo ""
fi
