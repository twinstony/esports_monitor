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
COPY main.py config.yaml config.example.yaml ./
COPY scripts/ ./scripts/

# 前端打包产物（已在本地构建好）
COPY webui/dist ./webui/dist

# 数据持久化目录
RUN mkdir -p /app/data
ENV DB_PATH=/app/data/esports_history.db

EXPOSE 8000

# 启动 Web 服务（API + 静态前端 + Monitor 后台线程）
CMD ["python", "main.py", "--mode", "web", "--host", "0.0.0.0", "--port", "8000"]
