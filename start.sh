#!/bin/sh
# 启动 AstrBot（前台运行，日志同时写入 logs/astrbot.log）
cd "$(dirname "$0")" || exit 1
export PATH=/workspace/.tools/node/bin:$PATH
exec .venv/bin/python main.py "$@"
