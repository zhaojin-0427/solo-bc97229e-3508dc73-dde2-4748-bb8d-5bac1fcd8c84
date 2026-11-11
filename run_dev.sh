#!/usr/bin/env bash
# 本地开发启动（无 Docker 环境时使用）。
# 依赖：pip install -r requirements.txt
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONUNBUFFERED=1
exec python3 -m gunicorn \
  --bind 0.0.0.0:8000 \
  --workers 1 --threads 8 --timeout 60 \
  --reload \
  app.web:app
