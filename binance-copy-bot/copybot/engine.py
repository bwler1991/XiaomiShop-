"""核心：把带单员持仓按比例换算成目标仓位，与我的仓位对账，差多少补多少。"""
import json, logging, os, time
from decimal import Decimal

log = logging.getLogger("copybot")

DEFAULTS = dict(
    capital_usdt=1000, lead_balance_usdt=None, max_loss_usdt=300, daily_max_loss_usdt=150,
    max_leverage=10, max_notional_per_symbol=5000, max_total_notional=None,
    max_entry_slippage_pct=1.0, symbol_whitelist=[], symbol_blacklist=[],
    dry_run=True, copy_existing_positions=False, poll_interval_sec=3, state_file="state.json",
    lead_host="www.binance.com", lead_state_file="lead_state.json")


class Engine:
    def __init__(self, cfg, lead, broker, notify=lambda m: None, persist=True):
        self.cfg = {**DEFAULTS, **cfg}
        self.lead, self.broker, self._notify, self.persist = lead, broker, notify, persist
        self.state = self._load()
        self.hedge = broker.hedge_mode()
        self.accepted = self.last_raw = None
        self.lead_balance, self._bal_ts = None, 0
        self._skipped, self._err = {}, {}

    # ---------- state ----------
    def _load(self):
        f = self.cfg["state_file"]
        if self.persist and os.path.exists(f):
            return json.load(open(f))
        return {"managed": []}

    def _save(self):
        if self.persist:
            tmp = self.cfg["state_file"] + ".tmp"
            json.dump(self.state, open(tmp, "w"), indent=1)
            os.replace(tmp, self.cfg["state_file"])

    def say(self, msg):
        log.info(msg)
        try:
            self._notify(msg)
        except Exception as e:
            log.warning("notify failed: %s", e)

    # ---------- sizing ----------
    def ratio(self):
        if time.time() - self._bal_ts > 600:
            self._bal_ts = time.time()
            b = self.lead.balance()
            if b:
                self.lead_balance = b
        bal = self.lead_balance or self.cfg["lead_balance_usdt"]
        if not bal:
            raise RuntimeError("拿不到带单员余额，请在 config 里填 lead_balance_usdt")
        return self.cfg["capital_usdt"] / bal

    def targets(self, lead_positions):
        """带单员仓位 → 我的目标仓位。

        比例在他每次【开仓/加仓】时锁定到那一部分仓位上（eff = 该仓位的有效比例）：
        - 加仓：只有新增部分按当前比例计算，已有部分不变；
        - 减仓：有效比例不变 → 我按同样的百分比减仓；
        - 他的余额变化（浮盈浮亏、提现）不会让已有仓位被动加减。
        单币种上限在加仓时生效（只限制新增），所以触顶后他减仓我仍按比例减。
        """
        c, r, base = self.cfg, self.ratio(), {tuple(k) for k in self.state["baseline"]}
        sized, live, out = self.state.setdefault("sized", {}), set(), {}
        for p in lead_positions:
            if (p.symbol, p.side) in base:
                continue
            if c["symbol_whitelist"] and p.symbol not in c["symbol_whitelist"]:
                continue
            if p.symbol in c["symbol_blacklist"]:
                continue
            k = f"{p.symbol}|{p.side}"
            live.add(k)
            prev = sized.get(k)
            if prev and prev["amt"] * p.amt <= 0:        # 反手：当作新仓
                prev = None
            a, pa = abs(p.amt), (abs(prev["amt"]) if prev else 0.0)
            if a > pa:                                   # 新开或加仓
                held = pa * prev["eff"] if prev else 0.0
                want = held + (a - pa) * r
                if p.mark > 0:
                    want = min(want, max(held, c["max_notional_per_symbol"] / p.mark))
                eff = want / a
            else:                                        # 减仓或不变
                eff = prev["eff"]
            sized[k] = {"amt": p.amt, "eff": eff}
            pside = "BOTH" if not self.hedge else (
                p.side if p.side != "BOTH" else ("LONG" if p.amt > 0 else "SHORT"))
            t = out.setdefault((p.symbol, pside), dict(qty=0.0, lev=1, ref=0.0, mark=p.mark))
            t["qty"] += p.amt * eff
            t["lev"] = max(1, min(p.leverage, c["max_leverage"]))
            t["ref"], t["mark"] = (getattr(p, "ref", 0.0) or p.entry), p.mark
        for k in [k for k in sized if k not in live]:
            del sized[k]
        return out

    # ---------- risk ----------
    def _close_all(self, mine):
        for k in [tuple(k) for k in self.state["managed"]]:
            cur = mine.get(k, 0.0)
            if cur:
                self._reduce(k, cur, abs(cur), full=True)
        self.state["managed"] = []

    def check_risk(self, lead_positions):
        """返回 True 表示本轮不再交易。"""
        st, c = self.state, self.cfg
        eq = self.broker.equity()
        if eq is None:
            return False
        day = time.strftime("%Y-%m-%d", time.gmtime())
        st.setdefault("start_equity", eq)
        if st.get("day") != day:
            st["day"], st["day_start_equity"] = day, eq
        total, daily = st["start_equity"] - eq, st["day_start_equity"] - eq
        if total >= c["max_loss_usdt"]:
            self._close_all(self.broker.positions())
            st["halted"] = True
            self.say(f"⛔ 累计亏损 {total:.2f} USDT ≥ 上限 {c['max_loss_usdt']}，已全部平仓并停止。"
                     f"恢复需运行: python -m copybot reset")
            return True
        if daily >= c["daily_max_loss_usdt"] and st.get("day_halt") != day:
            self._close_all(self.broker.positions())
            st["day_halt"] = day
            # 他当前的仓位不再追，等他平掉后才跟新仓
            st["baseline"] = sorted({tuple(k) for k in st["baseline"]} |
                                    {(p.symbol, p.side) for p in lead_positions})
            self.say(f"⏸ 当日亏损 {daily:.2f} USDT ≥ 上限 {c['daily_max_loss_usdt']}，已全部平仓，今日不再开仓。")
        return False

    # ---------- orders ----------
    def _reduce(self, key, cur, qty, full=False):
        sym, pside = key
        f = self.broker.filters.get(sym)
        if not f:
            return log.warning("%s 不在合约交易对列表里，无法平仓，请手动处理", sym)
        q = Decimal(str(abs(cur))) if full else min(round_step(qty, f[0]), Decimal(str(abs(cur))))
        if q <= 0:
            return
        side = "SELL" if cur > 0 else "BUY"
        self.broker.order(sym, side, q, pside, True)
        self.say(f"{'[模拟] ' if self.cfg['dry_run'] else ''}减/平仓 {sym} {pside} {side} {q}")

    def _open(self, key, tgt, qty, t, mine, tgts):
        sym, pside = key
        c = self.cfg
        if self.state.get("day_halt") == time.strftime("%Y-%m-%d", time.gmtime()):
            return
        f = self.broker.filters.get(sym)
        if not f:
            return self._skip(key, f"{sym} 不在 U 本位合约交易对里，跳过")
        mark, ref = t["mark"], t["ref"]      # ref = 他最近一次开/加仓的成交价
        if mark <= 0:
            return
        if ref > 0:
            dev = (mark / ref - 1) * 100 * (1 if tgt > 0 else -1)
            if dev > c["max_entry_slippage_pct"]:
                return self._skip(key, f"{sym} 现价比他最近一次开/加仓价不利 {dev:.2f}%，超过上限，暂不追")
        total_cap = c["max_total_notional"] or c["capital_usdt"] * c["max_leverage"]
        used = sum(abs(q) * tgts[k]["mark"] for k, q in mine.items() if k in tgts)
        qty = min(qty, max(0.0, total_cap - used) / mark)
        q = round_step(qty, f[0])
        if q < f[1] or float(q) * mark < float(f[2]):
            return self._skip(key, f"{sym} 按比例算出的数量 {qty:.6g} 低于交易所最小下单量，跳过"
                                   f"（本金太小或比例太低）")
        side = "BUY" if tgt > 0 else "SELL"
        try:
            self.broker.set_leverage(sym, t["lev"])
        except Exception as e:      # 部分品种杠杆上限更低；沿用账户现有杠杆继续下单
            log.warning("%s 设置杠杆 %sx 失败（沿用现有杠杆）: %s", sym, t["lev"], e)
        self.broker.order(sym, side, q, pside, False)
        self._skipped.pop(key, None)
        self.say(f"{'[模拟] ' if c['dry_run'] else ''}开/加仓 {sym} {pside} {side} {q} "
                 f"@~{mark} 杠杆{t['lev']}x")

    def _skip(self, key, msg):
        if self._skipped.get(key) != msg.split(" ")[1][:6]:   # 同类原因只提示一次
            self._skipped[key] = msg.split(" ")[1][:6]
            log.info(msg)

    # ---------- main step ----------
    def step(self):
        st = self.state
        if st.get("halted"):
            return
        lead = self.lead.positions()          # 失败会抛异常 → 本轮什么都不做
        raw = {(p.symbol, p.side) for p in lead}
        # 有仓位“消失”时要连续两次看到同样结果才相信，防止接口抖动导致误平仓
        if (not getattr(self.lead, "reliable_closes", False) and self.accepted is not None
                and (self.accepted - raw) and raw != self.last_raw):
            self.last_raw = raw
            return
        self.last_raw = self.accepted = raw

        if "baseline" not in st:
            st["baseline"] = [] if self.cfg["copy_existing_positions"] else sorted(raw)
            self.say(f"启动。跟单比例 {self.ratio():.5%}。只跟启动之后他新开的仓位。"
                     f"{' [模拟模式]' if self.cfg['dry_run'] else ''}")
        else:
            st["baseline"] = [k for k in st["baseline"] if tuple(k) in raw]

        if self.check_risk(lead):
            self._save()
            return
        tgts = self.targets(lead)
        mine = self.broker.positions()
        keys = set(tgts) | {tuple(k) for k in st["managed"]}
        for key in sorted(keys):
            cur, tgt = mine.get(key, 0.0), tgts.get(key, {}).get("qty", 0.0)
            same = cur * tgt > 0
            reduce_qty = max(0.0, abs(cur) - abs(tgt)) if same else abs(cur)
            open_qty = max(0.0, abs(tgt) - abs(cur)) if (same or cur == 0) else abs(tgt)
            try:
                if reduce_qty > 0:
                    self._reduce(key, cur, reduce_qty, full=(tgt == 0 or not same))
                if open_qty > 0:
                    self._open(key, tgt, open_qty, tgts[key], mine, tgts)
            except Exception as e:
                msg = f"⚠️ {key[0]} 下单失败: {e}"
                if time.time() - self._err.get(key, 0) > 600:    # 同一品种 10 分钟内只通知一次
                    self._err[key] = time.time()
                    self.say(msg)
                else:
                    log.warning(msg)
        after = self.broker.positions()
        st["managed"] = sorted(k for k in keys if after.get(k))
        self._save()


def round_step(qty, step):
    from .broker import round_qty
    return round_qty(qty, step)
