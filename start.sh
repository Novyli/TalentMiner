#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate 2>/dev/null || { echo "请先运行: bash setup.sh"; exit 1; }
echo "TalentMiner 启动中..."
echo "浏览器打开: http://127.0.0.1:8899"
python3 -u app.py
