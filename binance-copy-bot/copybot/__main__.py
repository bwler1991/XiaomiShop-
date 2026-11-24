import json, logging, os, sys, time
import requests, yaml
from .lead import LeadSource
from .broker import BinanceBroker, SimBroker
from .engine import Engine, DEFAULTS


def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)


def telegram():
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return lambda m: None
    return lambda m: requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                                   json={"chat_id": chat, "text": m}, timeout=10)


_px = {}


def mark_price(symbol):
    """官方公开行情：标记价格。失败返回 None（退回用他的成交价）。"""
    hit = _px.get(symbol)
    if hit and time.time() - hit[1] < 2:
        return hit[0]
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                         params={"symbol": symbol}, timeout=5).json()
        _px[symbol] = (float(r["markPrice"]), time.time())
        return _px[symbol][0]
    except Exception:
        return None


def report(logfile="copybot.log", n=25):
    """给人看的摘要（纯 ASCII，网页终端能正常显示）。"""
    import re
    if not os.path.exists(logfile):
        return print("no log yet")
    lines = open(logfile, errors="replace").read().splitlines()
    ev = [l.split(" | ")[0] for l in lines
          if re.search(r"\d (\[DRY\] )?(LEAD|OPEN|REDUCE|START|RUN) ", l)]
    lags = sorted(int(m.group(1)) for l in ev for m in [re.search(r"lag=(\d+)s", l)] if m)
    fails = sum(1 for l in lines if "POLL FAILED" in l or "\u672c\u8f6e\u5931\u8d25" in l)
    print(f"lead fills seen: {len(lags)}")
    if lags:
        print(f"lag seconds: min {lags[0]} / median {lags[len(lags) // 2]} / max {lags[-1]}")
        print("lag list:", " ".join(map(str, lags[-40:])))
    print(f"simulated or real orders: {sum(1 for l in ev if ' OPEN ' in l or ' REDUCE ' in l)}"
          f" | failed-poll log lines: {fails}")
    print(f"--- last {n} events ---")
    for l in ev[-n:]:
        print(l[5:19] + l[23:])          # 去掉年份和毫秒，行更短


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "report":
        return report()
    load_env()
    cfg = {**DEFAULTS, **(yaml.safe_load(open("config.yaml")) or {})}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("copybot.log")])
    log = logging.getLogger("copybot")
    if not cfg.get("portfolio_id"):
        sys.exit("请先在 config.yaml 里填 portfolio_id")
    lead = LeadSource(cfg["portfolio_id"], cfg["lead_host"], cfg["lead_state_file"], mark_price)

    if cmd == "check":      # 第一步：验证数据源
        d = lead.detail()
        print("LEAD balance now:", d.get("marginBalance"), "| positions public:", d.get("positionShow"),
              "|", d.get("nickname"))
        r = cfg["capital_usdt"] / cfg["lead_balance_usdt"]
        print(f"copy ratio: {r:.4%} (capital {cfg['capital_usdt']} / reference {cfg['lead_balance_usdt']})")
        print("== last 10 lead orders ==")
        for o in lead.orders(1, 10):
            t = time.strftime("%m-%d %H:%M:%S", time.gmtime(o["orderUpdateTime"] / 1000))
            print(f"{t} UTC  {o['symbol']:<12} {o.get('positionSide'):<5} {o['side']:<4} "
                  f"{o['executedQty']} @ {o['avgPrice']}  pnl={o.get('totalPnl')}")
        print("== Binance futures API:", "OK ==" if mark_price("BTCUSDT") else "FAILED (network/region) ==")
        return
    if cmd == "reset":      # 解除累计亏损熔断，重新计算起始权益
        if os.path.exists(cfg["state_file"]):
            st = json.load(open(cfg["state_file"]))
            for k in ("halted", "start_equity", "day_halt"):
                st.pop(k, None)
            json.dump(st, open(cfg["state_file"], "w"), indent=1)
        print("已重置熔断状态")
        return

    if cfg["dry_run"]:
        broker = SimBroker()
    else:
        key, sec = os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET")
        if not (key and sec):
            sys.exit("实盘需要在 .env 里填 BINANCE_API_KEY / BINANCE_API_SECRET")
        broker = BinanceBroker(key, sec)
    eng = Engine(cfg, lead, broker, telegram())
    log.info("RUN dry_run=%s | 开始运行", cfg["dry_run"])
    fails = 0
    while True:
        try:
            eng.step()
            fails = 0
        except Exception as e:
            fails += 1
            if fails == 1 or fails % 20 == 0:        # 接口偶尔返回“系统繁忙”，不刷屏
                log.warning("POLL FAILED x%d: %s", fails, e)
            if fails == 20:
                eng.say("⚠️ 连续 20 轮读取失败，跟单可能已中断，请检查")
        time.sleep(cfg["poll_interval_sec"] * min(1 + fails, 3))


if __name__ == "__main__":
    main()
