# ---- 前端构建 ----
FROM node:20-slim AS frontend
WORKDIR /build
COPY webui/package.json webui/package-lock.json ./
RUN npm ci
COPY webui/ ./
ENV NODE_OPTIONS=--max-old-space-size=4096
RUN NODE_OPTIONS=--max-old-space-size=4096 npm run build

# ---- 后端运行 ----
FROM python:3.12-slim
WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 项目代码
COPY esports_monitor/ ./esports_monitor/
COPY main.py config.example.yaml ./

# 前端打包产物
COPY --from=frontend /build/dist ./webui/dist

# 数据持久化目录
RUN mkdir -p /app/data
ENV DB_PATH=/app/data/esports_history.db

EXPOSE 8000

# 启动 Web 服务（API + 静态前端 + Monitor 后台线程）
CMD ["python", "main.py", "--mode", "web", "--host", "0.0.0.0", "--port", "8000"]
