#!/usr/bin/env bash
# 币安跟单机器人：一键安装 / 更新，并以【模拟模式】在后台持续运行。
# 可以重复运行：不会覆盖已有的 config.yaml、.env 和状态文件。
set -euo pipefail
BASE="${COPYBOT_BASE:-https://raw.githubusercontent.com/bwler1991/XiaomiShop-/ccr-7b07bd01-tyh3ic/binance-copy-bot}"
DIR="${COPYBOT_DIR:-/opt/copybot}"
FILES="requirements.txt config.example.yaml .env.example README.md
copybot/__init__.py copybot/__main__.py copybot/lead.py copybot/engine.py copybot/broker.py"
trap 'echo; echo "!!! 安装没有完成，请把这个屏幕截图发给助手 !!!"' ERR
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

echo "[1/5] 安装系统依赖（约 1 分钟）..."
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip curl >/dev/null

echo "[2/5] 下载程序..."
mkdir -p "$DIR/copybot"
for f in $FILES; do curl -fsSL "$BASE/$f" -o "$DIR/$f"; done

echo "[3/5] 安装 Python 依赖..."
cd "$DIR"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f config.yaml ] || cp config.example.yaml config.yaml
[ -f .env ] || cp .env.example .env
chmod 600 .env

echo "[4/5] 检查数据源..."
.venv/bin/python -m copybot check

echo "[5/5] 设置后台运行..."
cat > /usr/local/bin/copybot-log <<'EOT'
#!/bin/sh
echo "运行状态: $(systemctl is-active copybot 2>/dev/null || echo 未知)"
tail -n "${1:-30}" /opt/copybot/copybot.log
EOT
chmod +x /usr/local/bin/copybot-log
if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
  id copybot >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin copybot
  chown -R copybot: "$DIR"
  cat > /etc/systemd/system/copybot.service <<EOT
[Unit]
Description=Binance copy bot
After=network-online.target
Wants=network-online.target

[Service]
User=copybot
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m copybot run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOT
  systemctl daemon-reload
  systemctl enable -q copybot
  systemctl restart copybot
  sleep 5
  echo
  echo "=============================================="
  echo " 安装完成。运行状态: $(systemctl is-active copybot)"
  echo " 当前为模拟模式，不会下单。"
  echo " 以后查看运行情况，输入:  copybot-log"
  echo "=============================================="
  tail -n 5 "$DIR/copybot.log" || true
else
  echo "未检测到 systemd，已跳过后台运行设置。"
fi
