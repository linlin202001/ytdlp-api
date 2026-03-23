FROM python:3.11-slim

# 安装 ffmpeg（yt-dlp 合并音视频需要）
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Railway 会自动设置 PORT 环境变量
ENV PORT=8080

EXPOSE ${PORT}

CMD uvicorn server:app --host 0.0.0.0 --port ${PORT}
