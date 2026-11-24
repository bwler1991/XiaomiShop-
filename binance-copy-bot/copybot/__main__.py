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
          if re.search(r"\d (\[DRY\] )?(LEAD|OPEN|REDUCE|START|RUN|LEADTRIP|SIMTRIP|PROBE|HALT|PAUSE|ORDER FAILED) ", l)]
    lags = sorted(int(m.group(1)) for l in ev if " LEAD " in l
                  for m in [re.search(r"lag=(\d+)s", l)] if m)
    pl = sorted(int(m.group(1)) for l in ev if " PROBE " in l
                for m in [re.search(r"lag=(\d+)s", l)] if m)
    fails = sum(1 for l in lines if "POLL FAILED" in l or "\u672c\u8f6e\u5931\u8d25" in l)
    print(f"lead fills seen: {len(lags)}")
    if lags:
        print(f"lag seconds: min {lags[0]} / median {lags[len(lags) // 2]} / max {lags[-1]}")
        print("lag list:", " ".join(map(str, lags[-40:])))
    if pl:
        print(f"trade-history probe lag: min {pl[0]} / median {pl[len(pl) // 2]} / max {pl[-1]} ({len(pl)} fills)")
    for l in [l for l in ev if "TRIP " in l][-12:]:
        print(l[5:19] + l[23:])
    print(f"simulated or real orders: {sum(1 for l in ev if ' OPEN ' in l or ' REDUCE ' in l)}"
          f" | failed-poll log lines: {fails}")
    print(f"--- last {n} events ---")
    for l in ev[-n:]:
        print(l[5:19] + l[23:])          # 去掉年份和毫秒，行更短


CFG = "config.yaml"


def save_cfg(cfg):
    keep = {k: v for k, v in cfg.items() if k in DEFAULTS or k == "portfolio_id"}
    yaml.safe_dump(keep, open(CFG, "w"), allow_unicode=True, sort_keys=False)


def ask(prompt, default, cast=float):
    while True:
        v = input(f"{prompt} [{default}]: ").strip() or str(default)
        try:
            v = cast(v)
            if v > 0:
                return v
        except ValueError:
            pass
        print("  please type a number")


def summary(cfg):
    r = cfg["capital_usdt"] / cfg["lead_balance_usdt"]
    print("---------------- settings ----------------")
    print(f" mode:              {'DRY RUN (no real orders)' if cfg['dry_run'] else 'LIVE (REAL ORDERS)'}")
    print(f" capital:           {cfg['capital_usdt']:g} USDT")
    print(f" copy ratio:        {r:.4%}  (he opens 1,000,000 USDT -> you open {1e6 * r:,.0f} USDT)")
    print(f" stop for good at:  total loss {cfg['max_loss_usdt']:g} USDT")
    print(f" stop for the day:  daily loss {cfg['daily_max_loss_usdt']:g} USDT")
    print(f" leverage:          {cfg['max_leverage']}x")
    print(f" max per symbol:    {cfg['max_notional_per_symbol']:g} USDT position size")
    tot = cfg["max_total_notional"] or cfg["capital_usdt"] * cfg["max_leverage"]
    print(f" max all symbols:   {tot:g} USDT position size")
    print("-------------------------------------------")


def setup(cfg):
    print("Answer each question and press Enter. Just Enter keeps the value in [brackets].")
    cap = ask("1) Capital you put in, USDT", cfg["capital_usdt"])
    cfg["capital_usdt"] = cap
    cfg["max_loss_usdt"] = ask("2) Stop for good when TOTAL loss reaches, USDT", round(cap * 0.3))
    cfg["daily_max_loss_usdt"] = ask("3) Stop for the day when DAILY loss reaches, USDT", round(cap * 0.15))
    cfg["lead_balance_usdt"] = ask("4) Lead trader reference capital, USDT", 500000)
    cfg["max_leverage"] = ask("5) Leverage per symbol", cfg["max_leverage"], int)
    cfg["max_notional_per_symbol"] = round(cap * min(5, cfg["max_leverage"]))
    cfg["max_total_notional"] = None
    cfg["use_live_lead_balance"] = False
    save_cfg(cfg)
    summary(cfg)
    print("saved.")


def setkey():
    print("Paste or type your Binance API key and secret (futures enabled, withdrawals OFF).")
    key = input("API key: ").strip()
    sec = input("API secret: ").strip()
    for name, v in (("key", key), ("secret", sec)):
        if len(v) != 64 or not v.isalnum():
            print(f"  note: the {name} is {len(v)} characters; Binance ones are normally 64 letters/digits")
    old = [l.rstrip("\n") for l in open(".env")] if os.path.exists(".env") else []
    keep = [l for l in old if l and not l.startswith(("BINANCE_API_KEY=", "BINANCE_API_SECRET=", "#"))]
    open(".env", "w").write("\n".join([f"BINANCE_API_KEY={key}", f"BINANCE_API_SECRET={sec}"] + keep) + "\n")
    os.chmod(".env", 0o600)
    os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"] = key, sec
    print("saved.")


def live_broker(cfg):
    key, sec = os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET")
    if not (key and sec):
        sys.exit("no API key saved yet - run: copybot-key")
    return BinanceBroker(key, sec, cfg["max_notional_per_symbol"])


def livecheck(cfg):
    """只读检查：不下任何单。返回账户权益，失败返回 None。"""
    b = live_broker(cfg)
    try:
        hedge = b.hedge_mode()
        eq = b.equity()
        pos = b.positions()
    except Exception as e:
        print("ACCOUNT CHECK FAILED:", e)
        print("common causes: key/secret typo, futures permission not enabled on the key,")
        print("or the key's IP whitelist does not contain this server's IP.")
        return None
    print("ACCOUNT CHECK OK (read only, no orders sent)")
    print(f" futures account equity: {eq:.2f} USDT")
    print(f" position mode:          {'hedge (two-way)' if hedge else 'one-way'}")
    print(f" open positions now:     {len(pos)}")
    for k, v in list(pos.items())[:8]:
        print(f"   {k[0]} {k[1]} {v:g}")
    if eq < cfg["capital_usdt"] * 0.5:
        print(f" WARNING: equity is far below the capital setting ({cfg['capital_usdt']:g} USDT).")
        print("          move USDT into the futures wallet or lower the capital with copybot-setup.")
    return eq


def testorder(cfg, sym):
    """用最小金额真实下一笔并立刻平掉，验证下单代码。"""
    from decimal import Decimal, ROUND_UP
    b = live_broker(cfg)
    f, px = b.filters.get(sym), mark_price(sym)
    if not f or not px:
        sys.exit(f"cannot test with {sym}: symbol or price not available")
    step, minq, minn = f
    qty = max(minq, (Decimal(str(float(minn) * 1.3 / px)) / step).to_integral_value(ROUND_UP) * step)
    pside = "LONG" if b.hedge_mode() else "BOTH"
    print(f"TEST: buy {qty} {sym} (about {float(qty) * px:.2f} USDT), then sell it again right away.")
    print("      cost: a few cents of fees and spread.")
    if input("Type YES to send this real test order: ").strip() != "YES":
        return print("cancelled, nothing sent.")
    before = b.positions().get((sym, pside), 0.0)
    r = b.order(sym, "BUY", qty, pside, False, price=px)
    print(" buy sent  -> orderId", r.get("orderId"), r.get("status"))
    time.sleep(2)
    mid = b.positions().get((sym, pside), 0.0)
    print(f" position: {before:g} -> {mid:g}")
    r = b.order(sym, "SELL", qty, pside, True, price=px)
    print(" sell sent -> orderId", r.get("orderId"), r.get("status"))
    time.sleep(2)
    after = b.positions().get((sym, pside), 0.0)
    print(f" position: {mid:g} -> {after:g}")
    ok = abs(mid - before - float(qty)) < 1e-9 and abs(after - before) < 1e-9
    print("TEST OK - order code works on your account" if ok else
          "TEST DID NOT MATCH - send a screenshot of this screen; check the position in the Binance app")


def fresh_start(cfg):
    for f in (cfg["state_file"], cfg["lead_state_file"]):
        if os.path.exists(f):
            os.remove(f)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "report":
        return report()
    if cmd in ("setup", "setkey", "livecheck", "testorder", "golive", "pause", "show"):
        load_env()
        cfg = {**DEFAULTS, **(yaml.safe_load(open(CFG)) or {})}
        if cmd == "setup":
            return setup(cfg)
        if cmd == "show":
            return summary(cfg)
        if cmd == "setkey":
            setkey()
            return livecheck(cfg)
        if cmd == "livecheck":
            return livecheck(cfg)
        if cmd == "testorder":
            return testorder(cfg, sys.argv[2] if len(sys.argv) > 2 else "DOGEUSDT")
        if cmd == "pause":
            cfg["dry_run"] = True
            save_cfg(cfg)
            fresh_start(cfg)
            print("PAUSED: back to DRY RUN, no more real orders.")
            print("NOTE: positions that are already open on Binance stay open - close them in the app if you want.")
            return
        if cmd == "golive":
            summary(cfg)
            if livecheck(cfg) is None:
                sys.exit(1)
            print("From now on the bot copies every NEW trade of the lead trader with REAL money.")
            print("Positions he already holds right now are NOT copied.")
            if input("Type YES to start REAL trading: ").strip() != "YES":
                print("cancelled, still in the old mode.")
                sys.exit(1)
            cfg["dry_run"] = False
            save_cfg(cfg)
            fresh_start(cfg)
            print("LIVE mode saved.")
            return
    load_env()
    cfg = {**DEFAULTS, **(yaml.safe_load(open("config.yaml")) or {})}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("copybot.log")])
    log = logging.getLogger("copybot")
    if not cfg.get("portfolio_id"):
        sys.exit("请先在 config.yaml 里填 portfolio_id")
    lead = LeadSource(cfg["portfolio_id"], cfg["lead_host"], cfg["lead_state_file"], mark_price,
                      probe=bool(cfg["dry_run"] and cfg.get("probe_trade_history", True)))

    if cmd == "check":      # 第一步：验证数据源
        try:
            d = lead.detail()
        except Exception as e:
            d = {}
            print("(could not read lead profile right now - temporary):", e)
        print("LEAD balance now:", d.get("marginBalance"), "| positions public:", d.get("positionShow"),
              "|", d.get("nickname"))
        r = cfg["capital_usdt"] / cfg["lead_balance_usdt"]
        print(f"copy ratio: {r:.4%} (capital {cfg['capital_usdt']} / reference {cfg['lead_balance_usdt']})")
        print("== last 10 lead orders ==")
        try:
            for o in lead.orders(1, 10, tries=8):
                t = time.strftime("%m-%d %H:%M:%S", time.gmtime(o["orderUpdateTime"] / 1000))
                print(f"{t} UTC  {o['symbol']:<12} {o.get('positionSide'):<5} {o['side']:<4} "
                      f"{o['executedQty']} @ {o['avgPrice']}  pnl={o.get('totalPnl')}")
        except Exception as e:      # 临时性错误不算失败，机器人运行时会自动重试
            print("(could not read lead orders right now - temporary, the bot retries by itself):", e)
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
        broker = BinanceBroker(key, sec, cfg["max_notional_per_symbol"])
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
