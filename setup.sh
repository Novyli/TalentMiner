#!/bin/bash
echo "=== TalentMiner 安装 ==="
if ! command -v python3 &> /dev/null; then
    echo "错误: 需要 Python 3.9+"
    exit 1
fi
echo "Python: $(python3 --version)"
echo "创建虚拟环境..."
python3 -m venv venv
source venv/bin/activate
echo "安装依赖..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "安装完成!"
echo "运行: bash start.sh"
echo "浏览器打开 http://127.0.0.1:8899"
