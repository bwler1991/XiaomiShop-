"""币安 U 本位合约官方 API（下单端）+ 模拟盘。"""
import hashlib, hmac, logging, time, urllib.parse
from decimal import Decimal, ROUND_DOWN
import requests

FAPI = "https://fapi.binance.com"
log = logging.getLogger("copybot")


class Filters:
    def __init__(self, s=None):
        self.symbols = {}
        self.s = s or requests.Session()

    def load(self):
        info = self.s.get(FAPI + "/fapi/v1/exchangeInfo", timeout=15).json()
        for sym in info["symbols"]:
            if sym.get("status") != "TRADING":
                continue
            f = {x["filterType"]: x for x in sym["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]
            self.symbols[sym["symbol"]] = (
                Decimal(lot["stepSize"]), Decimal(lot["minQty"]),
                Decimal(f.get("MIN_NOTIONAL", {}).get("notional", "5")))

    def get(self, symbol):
        if not self.symbols:
            self.load()
        return self.symbols.get(symbol)


def round_qty(qty, step):
    return (Decimal(str(qty)) / step).to_integral_value(ROUND_DOWN) * step


class BinanceBroker:
    def __init__(self, key, secret):
        self.key, self.secret = key, secret.encode()
        self.s = requests.Session()
        self.s.headers["X-MBX-APIKEY"] = key
        self.filters = Filters(self.s)
        self._lev = {}

    def _req(self, method, path, **params):
        params.update(timestamp=int(time.time() * 1000), recvWindow=5000)
        q = urllib.parse.urlencode(params)
        sig = hmac.new(self.secret, q.encode(), hashlib.sha256).hexdigest()
        r = self.s.request(method, f"{FAPI}{path}?{q}&signature={sig}", timeout=10)
        if r.status_code >= 400:
            raise RuntimeError(f"binance {path} {r.status_code}: {r.text[:300]}")
        return r.json()

    def hedge_mode(self):
        return bool(self._req("GET", "/fapi/v1/positionSide/dual")["dualSidePosition"])

    def positions(self):
        out = {}
        for p in self._req("GET", "/fapi/v2/positionRisk"):
            amt = float(p["positionAmt"])
            if amt:
                out[(p["symbol"], p.get("positionSide", "BOTH"))] = amt
        return out

    def equity(self):
        return float(self._req("GET", "/fapi/v2/account")["totalMarginBalance"])

    def set_leverage(self, symbol, lev):
        if self._lev.get(symbol) != lev:
            self._req("POST", "/fapi/v1/leverage", symbol=symbol, leverage=lev)
            self._lev[symbol] = lev

    def order(self, symbol, side, qty, pside, reduce_only, price=None):
        p = dict(symbol=symbol, side=side, type="MARKET", quantity=format(qty, "f"))
        if pside != "BOTH":
            p["positionSide"] = pside
        elif reduce_only:
            p["reduceOnly"] = "true"
        return self._req("POST", "/fapi/v1/order", **p)


class SimBroker:
    """dry_run 用：不碰账户，只在内存里记仓位，并按发现时的标记价格估算每笔交易的收益。"""
    def __init__(self, filters=None, hedge=False):
        self.filters = filters or Filters()
        self.pos, self.orders, self._hedge, self.book = {}, [], hedge, {}

    def hedge_mode(self): return self._hedge
    def positions(self): return {k: v for k, v in self.pos.items() if v}
    def equity(self): return None
    def set_leverage(self, symbol, lev): pass

    def order(self, symbol, side, qty, pside, reduce_only, price=None):
        k = (symbol, pside)
        old = self.pos.get(k, 0.0)
        new = round(old + (float(qty) if side == "BUY" else -float(qty)), 12)
        self.pos[k] = new
        self.orders.append((symbol, side, float(qty), pside, reduce_only))
        if not price:
            return
        b = self.book.setdefault(k, dict(entry=0.0, x_qty=0.0, x_not=0.0))
        if abs(new) > abs(old):
            b["entry"] = (abs(old) * b["entry"] + (abs(new) - abs(old)) * price) / abs(new)
        else:
            b["x_qty"] += abs(old) - abs(new)
            b["x_not"] += (abs(old) - abs(new)) * price
        if new == 0:
            if b["x_qty"] and b["entry"] > 0:
                ex = b["x_not"] / b["x_qty"]
                ret = (ex / b["entry"] - 1) * (1 if old > 0 else -1) * 100
                log.info("SIMTRIP %s %s entry=%.6g exit=%.6g ret=%+.2f%%", symbol, pside, b["entry"], ex, ret)
            del self.book[k]
