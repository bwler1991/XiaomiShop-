"""读取带单员的公开成交记录，并据此还原他（自机器人启动之后新开的）仓位。

注意：这是币安网页的内部接口，非官方 API，可能随时变更。
该带单员隐藏了“持有仓位”（positionShow=false），所以只能用“最新交易记录”(order-history)。
"""
import json, logging, os, time
from dataclasses import dataclass
import requests

log = logging.getLogger("copybot")
PATH = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json", "Content-Type": "application/json", "clienttype": "web",
}


@dataclass
class LeadPos:
    symbol: str
    side: str      # BOTH / LONG / SHORT
    amt: float     # 带符号：多为正，空为负
    entry: float
    mark: float
    leverage: int = 125   # 成交记录里没有杠杆，实际杠杆由 max_leverage 决定
    ref: float = 0.0      # 他最近一次开/加仓的成交价（判断是否追价用）


class LeadSource:
    reliable_closes = True    # 平仓来自明确的成交记录，不需要二次确认

    def __init__(self, portfolio_id, host="www.binance.com", state_file="lead_state.json",
                 price_fn=None, persist=True, session=None):
        self.pid, self.base = str(portfolio_id), f"https://{host}{PATH}"
        self.state_file, self.persist, self.price_fn = state_file, persist, price_fn
        self.s = session or requests.Session()
        self.st = {"seen": {}, "pos": {}}
        if persist and os.path.exists(state_file):
            self.st = json.load(open(state_file))

    # ---------- http ----------
    def _check(self, r):
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            raise RuntimeError(f"lead api error: {j.get('code')} {j.get('message')}")
        return j.get("data")

    def detail(self):
        return self._check(self.s.get(f"{self.base}/detail", params={"portfolioId": self.pid},
                                      headers=HEADERS, timeout=10)) or {}

    def orders(self, page=1, size=20):
        d = self._check(self.s.post(f"{self.base}/order-history", headers=HEADERS, timeout=10,
                                    json={"portfolioId": self.pid, "pageNumber": page, "pageSize": size}))
        lst = (d or {}).get("list")
        if not isinstance(lst, list):
            raise RuntimeError(f"unexpected order-history payload: {str(d)[:200]}")
        return lst

    def balance(self):
        try:
            v = float(self.detail().get("marginBalance") or 0)
            return v if v > 0 else None
        except Exception:
            return None

    # ---------- 成交 → 仓位 ----------
    def _poll_new(self):
        st, seen, new = self.st, self.st["seen"], []
        first = "init_ts" not in st
        newest = 0
        for page in range(1, 11):
            lst, hit_old = self.orders(page), False
            for o in lst:
                k = "|".join(str(o.get(x)) for x in ("orderTime", "symbol", "side", "positionSide", "type"))
                q, upd = float(o["executedQty"]), int(o["orderUpdateTime"])
                newest = max(newest, upd)
                prev = seen.get(k)
                if first:
                    seen[k] = [q, upd]
                elif prev is None and upd <= st["init_ts"]:
                    hit_old = True                       # 启动前的旧单
                elif prev is not None and q <= prev[0] + 1e-12:
                    hit_old = True                       # 已处理过
                else:
                    new.append((o, q - (prev[0] if prev else 0.0)))   # 新单，或限价单又成交了一部分
                    seen[k] = [q, upd]
            if first or hit_old or not lst:
                break
        if first:
            st["init_ts"] = newest or int(time.time() * 1000)
        if len(seen) > 3000:                             # 只保留最近的
            for k in sorted(seen, key=lambda k: seen[k][1])[:-2000]:
                del seen[k]
        return sorted(new, key=lambda x: x[0]["orderUpdateTime"])

    def _apply(self, o, qty):
        sym, ps, buy = o["symbol"], o.get("positionSide", "BOTH"), o["side"] == "BUY"
        price = float(o.get("avgPrice") or 0)
        key = f"{sym}|{ps}"
        p = self.st["pos"].setdefault(key, {"amt": 0.0, "entry": 0.0, "last": price})
        p["last"] = price
        d = qty if buy else -qty                         # 带符号变化量
        amt = p["amt"]
        if ps == "LONG":
            new = max(0.0, amt + d)
        elif ps == "SHORT":
            new = min(0.0, amt + d)
        else:  # 单向持仓：看不到他启动前的仓位，用“有已实现盈亏=在平仓”来区分
            closing = float(o.get("totalPnl") or 0) != 0
            if amt == 0 or amt * d > 0:
                new = amt if closing else amt + d        # 在平启动前的老仓 → 忽略
            else:
                new = amt + d
                if new * amt < 0:
                    new = 0.0                            # 不跟反手多出来的部分（无法确认）
        if abs(new) > abs(amt) and price > 0:            # 加仓：更新均价
            p["entry"] = (abs(amt) * p["entry"] + (abs(new) - abs(amt)) * price) / abs(new)
            p["ref"] = price
        p["amt"] = round(new, 10)
        lag = time.time() - int(o["orderUpdateTime"]) / 1000
        log.info("LEAD %s %s %s qty=%.6g px=%s tracked=%.6g lag=%.0fs | 带单员成交（发现延迟 %.0f 秒）",
                 sym, ps, o["side"], qty, price, p["amt"], lag, lag)
        if p["amt"] == 0:
            del self.st["pos"][key]

    def positions(self):
        for o, qty in self._poll_new():
            self._apply(o, qty)
        if self.persist:
            tmp = self.state_file + ".tmp"
            json.dump(self.st, open(tmp, "w"))
            os.replace(tmp, self.state_file)
        out = []
        for key, p in self.st["pos"].items():
            sym, ps = key.split("|")
            mark = (self.price_fn(sym) if self.price_fn else None) or p["last"]
            out.append(LeadPos(sym, ps, p["amt"], p["entry"], mark, ref=p.get("ref", 0.0)))
        return out
