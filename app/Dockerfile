FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IDENTITY_DB_PATH=/app/data/identity.db

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
RUN mkdir -p /app/data

EXPOSE 8000

# gunicorn 生产级启动；溯源/归并为纯 CPU + 本地 SQLite，2 进程 × 4 线程足够演示。
# SQLite 写入由 BEGIN IMMEDIATE + busy_timeout 串行化，跨进程并发安全。
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "60", "server:app"]
