FROM python:3.13-slim
# Works on Railway (volume at /data) and Render free (ephemeral fs; set PORT + DESK_DB_REMOTE=1 via env).
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DESK_DB_PATH=/data/desk.db DESK_LOG_DIR=/data/logs
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data/logs
CMD ["python", "run.py"]
