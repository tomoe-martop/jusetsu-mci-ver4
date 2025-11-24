FROM node:20-slim

WORKDIR /app

# package.jsonとpackage-lock.jsonをコピー
COPY package*.json ./

# 依存関係をインストール
RUN npm ci --only=production

# アプリケーションのソースをコピー
COPY server.js ./
COPY test_api_data ./test_api_data/

# ポートを公開（Cloud Runは環境変数PORTを使用）
ENV PORT=8080
EXPOSE 8080

# アプリケーションを起動
CMD ["node", "server.js"]