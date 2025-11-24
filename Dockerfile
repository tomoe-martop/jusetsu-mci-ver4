FROM python:3.12-slim

# 作業ディレクトリを設定
WORKDIR /app

# システムの依存関係をインストール（LightGBMに必要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# requirements.txtをコピーして依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションのソースコードをコピー
COPY . .

# /tmp/dataディレクトリを作成
RUN mkdir -p /tmp/data

# アプリケーションを実行
CMD ["python", "main.py"]
