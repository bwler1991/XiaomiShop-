#!/usr/bin/env bash
# 币安跟单机器人：一键安装 / 更新，并以【模拟模式】在后台持续运行。
# 可以重复运行：不会覆盖已有的 config.yaml、.env 和状态文件。
set -euo pipefail
BASE="${COPYBOT_BASE:-https://raw.githubusercontent.com/bwler1991/XiaomiShop-/ccr-7b07bd01-tyh3ic/binance-copy-bot}"
DIR="${COPYBOT_DIR:-/opt/copybot}"
FILES="requirements.txt config.example.yaml .env.example README.md
copybot/__init__.py copybot/__main__.py copybot/lead.py copybot/engine.py copybot/broker.py"
trap 'echo; echo "!!! INSTALL FAILED - send a screenshot of this screen !!!"' ERR
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

echo "[1/5] installing system packages (about 1 min)..."
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip curl >/dev/null

echo "[2/5] downloading program..."
mkdir -p "$DIR/copybot"
for f in $FILES; do curl -fsSL "$BASE/$f?t=$(date +%s)" -o "$DIR/$f"; done

echo "[3/5] installing Python packages..."
cd "$DIR"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f config.yaml ] || cp config.example.yaml config.yaml
[ -f .env ] || cp .env.example .env
chmod 600 .env

echo "[4/5] checking data source..."
.venv/bin/python -m copybot check || echo "(check reported a problem - continuing anyway)"

echo "[5/5] setting up background service..."
cat > /usr/local/bin/copybot-log <<EOT
#!/bin/sh
echo "status: \$(systemctl is-active copybot 2>/dev/null || echo unknown)"
cd $DIR && .venv/bin/python -m copybot report
EOT
mk() {  # mk <command-name> <copybot subcommand> <restart service afterwards: yes|no>
  cat > "/usr/local/bin/$1" <<EOT
#!/bin/sh
cd $DIR || exit 1
.venv/bin/python -m copybot $2 "\$@" || exit 1
chown -R copybot: $DIR 2>/dev/null
[ "$3" = yes ] && systemctl restart copybot && sleep 3 && echo "service: \$(systemctl is-active copybot)"
exit 0
EOT
  chmod +x "/usr/local/bin/$1"
}
mk copybot-setup setup yes      # capital / loss limits
mk copybot-key   setkey no      # save API key + read-only account check
mk copybot-check livecheck no   # read-only account check
mk copybot-test  testorder no   # tiny real order, bought and sold again
mk copybot-live  golive yes     # switch to REAL trading (asks for YES)
mk copybot-pause pause yes      # back to dry run
mk copybot-show  show no        # print current settings
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
  echo " INSTALL OK. status: $(systemctl is-active copybot)"
  echo " mode: $(grep -q '^dry_run: false' "$DIR/config.yaml" && echo 'LIVE (REAL ORDERS)' || echo 'DRY RUN (no real orders)')"
  echo " commands: copybot-log  copybot-setup  copybot-key  copybot-test  copybot-live  copybot-pause"
  echo "=============================================="
  tail -n 5 "$DIR/copybot.log" || true
else
  echo "no systemd found, background service skipped."
fi
