#!/usr/bin/env bash
# 便捷启动：默认同时开启真实作业与模拟演练。
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
MODE="${MODE:-both}"
DATA="${DATA:-.ops-data/state.json}"
exec python3 -m opshost.main --mode "$MODE" --port "$PORT" --data "$DATA"
