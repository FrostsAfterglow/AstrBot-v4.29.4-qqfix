#!/bin/sh
# 在本沙盒中启动 AstrBot 并保持会话（日志 -> /tmp/astrbot.log）
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python main.py > /tmp/astrbot.log 2>&1
