from decimal import Decimal
from copybot.lead import LeadPos
from copybot.broker import SimBroker
from copybot.engine import Engine


class FakeFilters:
    def get(self, s):
        return None if s == "NOPEUSDT" else (Decimal("0.001"), Decimal("0.001"), Decimal("5"))


class FakeLead:
    def __init__(self): self.pos = []
    def positions(self): return list(self.pos)
    def balance(self): return 100000.0


class EqBroker(SimBroker):
    eq = 1000.0
    def equity(self): return self.eq


def mk(broker=None, **cfg):
    lead = FakeLead()
    b = broker or SimBroker(FakeFilters())
    msgs = []
    e = Engine({"capital_usdt": 1000, "use_live_lead_balance": True, **cfg}, lead, b, msgs.append, persist=False)
    return e, lead, b, msgs


def P(sym, amt, entry=2000.0, mark=2000.0, lev=20, side="BOTH"):
    return LeadPos(sym, side, amt, entry, mark, lev)


def test_open_add_reduce_close():
    e, lead, b, _ = mk()
    e.step()                                   # 启动，空仓
    lead.pos = [P("ETHUSDT", 100)]             # 比例 1%，→ 1 ETH
    e.step()
    assert b.positions() == {("ETHUSDT", "BOTH"): 1.0}
    lead.pos = [P("ETHUSDT", 150)]
    e.step()
    assert b.positions()[("ETHUSDT", "BOTH")] == 1.5
    lead.pos = [P("ETHUSDT", 50)]
    e.step()
    assert b.positions()[("ETHUSDT", "BOTH")] == 0.5
    assert b.orders[-1][4] is True             # reduceOnly
    lead.pos = []
    e.step()                                   # 第一次看到消失：等确认（仅持仓快照型数据源）
    assert b.positions()
    e.step()
    assert b.positions() == {}


def test_existing_positions_ignored_until_closed():
    e, lead, b, _ = mk()
    lead.pos = [P("ETHUSDT", 100)]
    e.step(); e.step()
    assert b.positions() == {}
    lead.pos = []
    e.step(); e.step()
    lead.pos = [P("ETHUSDT", -100)]
    e.step()
    assert b.positions() == {("ETHUSDT", "BOTH"): -1.0}


def test_flip_and_caps_and_slippage():
    e, lead, b, _ = mk(max_notional_per_symbol=1000)
    e.step()
    lead.pos = [P("ETHUSDT", 100)]             # 目标 1 ETH=2000U，被上限压到 0.5
    e.step()
    assert b.positions()[("ETHUSDT", "BOTH")] == 0.5
    lead.pos = [P("ETHUSDT", -100)]            # 反手
    e.step()
    assert b.positions()[("ETHUSDT", "BOTH")] == -0.5
    lead.pos = [P("ETHUSDT", -100), P("BTCUSDT", 10, entry=100.0, mark=103.0)]
    e.step()                                   # 现价比均价高 3% → 不追
    assert ("BTCUSDT", "BOTH") not in b.positions()
    lead.pos = [P("ETHUSDT", -100), P("NOPEUSDT", 10)]
    e.step(); e.step()                         # 未知交易对不报错


def test_hedge_mode_and_too_small():
    e, lead, b, _ = mk(broker=SimBroker(FakeFilters(), hedge=True))
    e.step()
    lead.pos = [P("ETHUSDT", 100, side="LONG"), P("ETHUSDT", -40, side="SHORT"),
                P("BTCUSDT", 0.01, mark=60000.0, entry=60000.0)]   # 0.0001 BTC 太小
    e.step()
    assert b.positions() == {("ETHUSDT", "LONG"): 1.0, ("ETHUSDT", "SHORT"): -0.4}


def test_max_loss_halts_and_closes():
    br = EqBroker(FakeFilters())
    e, lead, b, msgs = mk(broker=br, max_loss_usdt=300, daily_max_loss_usdt=9999)
    e.step()
    lead.pos = [P("ETHUSDT", 100)]
    e.step()
    assert b.positions()
    br.eq = 690.0
    e.step()
    assert b.positions() == {} and e.state["halted"]
    lead.pos = [P("ETHUSDT", 100), P("BTCUSDT", 100)]
    e.step()
    assert b.positions() == {}


def test_daily_loss_pauses_opens():
    br = EqBroker(FakeFilters())
    e, lead, b, _ = mk(broker=br, max_loss_usdt=9999, daily_max_loss_usdt=100)
    e.step()
    lead.pos = [P("ETHUSDT", 100)]
    e.step()
    br.eq = 880.0
    e.step()
    assert b.positions() == {}
    lead.pos = [P("ETHUSDT", 100), P("BTCUSDT", 100)]
    e.step()
    assert b.positions() == {}                 # 当天不再开仓


# ---------- 成交记录 → 仓位 ----------
from copybot.lead import LeadSource


def O(t, sym, side, ps, qty, price=100.0, pnl=0.0, upd=None, typ="MARKET"):
    return dict(symbol=sym, side=side, positionSide=ps, type=typ, executedQty=qty,
                avgPrice=price, totalPnl=pnl, orderTime=t, orderUpdateTime=upd or t)


def src(feed):
    s = LeadSource("1", persist=False)
    s.orders = lambda page=1, size=20: list(reversed(feed)) if page == 1 else []
    s.balance = lambda: 100000.0
    return s


def test_order_history_tracking():
    feed = [O(1, "NVDAUSDT", "BUY", "LONG", 50)]           # 启动前的老单
    s = src(feed)
    assert s.positions() == []                              # 首次：只记账不跟
    feed.append(O(10, "BTCUSDT", "BUY", "LONG", 2, 100))
    feed.append(O(11, "BTCUSDT", "BUY", "LONG", 2, 200))
    p = s.positions()
    assert (p[0].symbol, p[0].side, p[0].amt, p[0].entry) == ("BTCUSDT", "LONG", 4, 150)
    assert s.positions()[0].amt == 4                        # 重复轮询不重复计
    feed[-1] = O(11, "BTCUSDT", "BUY", "LONG", 3, 200, upd=12)   # 限价单继续成交
    assert s.positions()[0].amt == 5
    feed.append(O(20, "NVDAUSDT", "SELL", "LONG", 50, pnl=9))    # 平启动前的老仓 → 忽略
    feed.append(O(21, "ETHUSDT", "SELL", "SHORT", 10))
    feed.append(O(22, "BTCUSDT", "SELL", "LONG", 5, pnl=5))
    p = {(x.symbol, x.side): x.amt for x in s.positions()}
    assert p == {("ETHUSDT", "SHORT"): -10}
    feed.append(O(30, "ETHUSDT", "BUY", "SHORT", 10, pnl=1))
    assert s.positions() == []


def test_end_to_end_with_order_feed():
    feed = []
    s = src(feed)
    b = SimBroker(FakeFilters(), hedge=True)
    e = Engine({"capital_usdt": 1000, "lead_balance_usdt": 100000}, s, b, persist=False)
    e.step()
    T = s.st["init_ts"]                                     # 空记录启动时取当前时间
    feed.append(O(T + 10, "ETHUSDT", "SELL", "SHORT", 100, 2000.0))
    e.step()
    assert b.positions() == {("ETHUSDT", "SHORT"): -1.0}
    feed.append(O(T + 20, "ETHUSDT", "BUY", "SHORT", 100, 1900.0, pnl=1))
    e.step()                                                # 平仓立即跟，不等二次确认
    assert b.positions() == {}


def test_adds_and_reduces_are_proportional():
    e, lead, b, _ = mk(max_notional_per_symbol=1e9, max_total_notional=1e9)
    e.step()
    lead.pos = [P("ETHUSDT", 100)]                 # 比例 1% → 1 ETH
    e.step()
    n = len(b.orders)
    e.lead_balance = 50000.0                       # 他提现一半 → 比例变 2%
    e.step(); e.step()
    assert len(b.orders) == n                      # 已有仓位不被动加减
    lead.pos = [P("ETHUSDT", 150)]                 # 加仓 50：只有新增部分按 2% → +1
    e.step()
    assert b.positions()[("ETHUSDT", "BOTH")] == 2.0
    lead.pos = [P("ETHUSDT", 75)]                  # 他减一半 → 我也减一半
    e.step()
    assert b.positions()[("ETHUSDT", "BOTH")] == 1.0
    lead.pos = [P("ETHUSDT", 15)]                  # 再减 80%
    e.step()
    assert b.positions()[("ETHUSDT", "BOTH")] == 0.2


def test_cap_binds_on_add_but_reduce_stays_proportional():
    e, lead, b, _ = mk(max_notional_per_symbol=3000)   # 上限 1.5 ETH
    e.step()
    lead.pos = [P("ETHUSDT", 100)]
    e.step()
    lead.pos = [P("ETHUSDT", 300)]                 # 按比例应为 3 ETH，被上限压到 1.5
    e.step()
    assert b.positions()[("ETHUSDT", "BOTH")] == 1.5
    lead.pos = [P("ETHUSDT", 150)]                 # 他减一半 → 我减到 0.75，而不是原地不动
    e.step()
    assert b.positions()[("ETHUSDT", "BOTH")] == 0.75


def test_small_adds_accumulate_and_slippage_uses_last_add_price():
    e, lead, b, _ = mk()
    e.step()
    lead.pos = [P("BTCUSDT", 0.004, entry=60000.0, mark=60000.0)]   # ×1% = 0.00004，太小
    e.step()
    assert b.positions() == {}
    lead.pos = [P("BTCUSDT", 0.2, entry=60000.0, mark=60000.0)]     # 累计够了 → 一次补齐
    e.step()
    assert b.positions()[("BTCUSDT", "BOTH")] == 0.002
    # 他在高位加仓：均价 60000，最近加仓价 66000，现价 66100 → 相对加仓价只差 0.15%，应该跟
    lp = P("BTCUSDT", 0.4, entry=63000.0, mark=66100.0); lp.ref = 66000.0
    lead.pos = [lp]
    e.step()
    assert b.positions()[("BTCUSDT", "BOTH")] == 0.004


def test_ratio_ignores_live_balance_by_default():
    lead = FakeLead()
    lead.balance = lambda: 500.0                   # 他把钱提走后只剩 500
    b = SimBroker(FakeFilters())
    e = Engine({"capital_usdt": 1000, "lead_balance_usdt": 500000}, lead, b, persist=False)
    assert e.ratio() == 0.002
    e.step()
    lead.pos = [P("ETHUSDT", 500)]                 # 他开 100 万 U → 我开 1 ETH = 2000 U，而不是按 200% 开
    e.step()
    assert b.positions() == {("ETHUSDT", "BOTH"): 1.0}


# ---------- 实盘下单端：用假的 HTTP 会话检查请求参数 ----------
from copybot.broker import BinanceBroker


class FakeHTTP:
    def __init__(self, hedge, v3=True):
        self.hedge, self.v3, self.calls, self.pos, self.headers = hedge, v3, [], {}, {}

    def request(self, method, url, timeout=None):
        import urllib.parse
        path, q = url.split("?", 1)
        path = path.replace("https://fapi.binance.com", "")
        p = dict(urllib.parse.parse_qsl(q))
        assert "signature" in p and "timestamp" in p
        self.calls.append((method, path, p))

        class R:
            status_code, text = 200, ""
            def json(s): return body
        if path.startswith("/fapi/v3/") and not self.v3:
            R.status_code, R.text, body = 404, "not found", {}
        elif path.endswith("/positionSide/dual"):
            body = {"dualSidePosition": self.hedge}
        elif path.endswith("/account"):
            body = {"totalMarginBalance": "1234.5"}
        elif path.endswith("/positionRisk"):
            body = [{"symbol": s, "positionSide": ps, "positionAmt": str(a)} for (s, ps), a in self.pos.items()]
        elif path.endswith("/order"):
            k = (p["symbol"], p.get("positionSide", "BOTH"))
            self.pos[k] = self.pos.get(k, 0) + float(p["quantity"]) * (1 if p["side"] == "BUY" else -1)
            body = {"orderId": 1, "status": "NEW"}
        else:
            body = {}
        return R()


def live(hedge, v3=True):
    b = BinanceBroker("k", "s", max_order_notional=5000)
    b.s = FakeHTTP(hedge, v3)
    b.filters = FakeFilters()
    return b


def test_live_broker_requests_one_way_and_hedge():
    b = live(False)
    assert b.hedge_mode() is False and b.equity() == 1234.5
    b.order("ETHUSDT", "BUY", Decimal("0.5"), "BOTH", False, price=2000.0)
    b.order("ETHUSDT", "SELL", Decimal("0.5"), "BOTH", True, price=2000.0)
    o1, o2 = [c[2] for c in b.s.calls if c[1] == "/fapi/v1/order"]
    assert o1["type"] == "MARKET" and o1["quantity"] == "0.5" and "reduceOnly" not in o1 and "positionSide" not in o1
    assert o2["reduceOnly"] == "true"
    h = live(True, v3=False)                       # 双向持仓 + v3 不存在时退回 v2
    assert h.hedge_mode() is True and h.equity() == 1234.5
    h.order("ETHUSDT", "SELL", Decimal("0.5"), "SHORT", False, price=2000.0)
    h.order("ETHUSDT", "BUY", Decimal("0.5"), "SHORT", True, price=2000.0)
    o1, o2 = [c[2] for c in h.s.calls if c[1] == "/fapi/v1/order"]
    assert o1["positionSide"] == "SHORT" and o2["positionSide"] == "SHORT"
    assert "reduceOnly" not in o1 and "reduceOnly" not in o2
    assert h.positions() == {}
    assert any(c[1] == "/fapi/v2/account" for c in h.s.calls)


def test_live_broker_refuses_oversized_open():
    b = live(False)
    try:
        b.order("ETHUSDT", "BUY", Decimal("10"), "BOTH", False, price=2000.0)   # 20000 > 5000
        assert False
    except RuntimeError as e:
        assert "refused" in str(e)
    assert not [c for c in b.s.calls if c[1] == "/fapi/v1/order"]


def test_engine_live_cycle_in_hedge_mode():
    feed = []
    s = src(feed)
    b = live(True)
    e = Engine({"capital_usdt": 1000, "lead_balance_usdt": 100000, "dry_run": False}, s, b, persist=False)
    e.step()
    T = s.st["init_ts"]
    feed.append(O(T + 10, "ETHUSDT", "SELL", "SHORT", 100, 2000.0))
    e.step()
    assert b.positions() == {("ETHUSDT", "SHORT"): -1.0}
    feed.append(O(T + 20, "ETHUSDT", "BUY", "SHORT", 40, 1950.0, pnl=1))
    e.step()
    assert round(b.positions()[("ETHUSDT", "SHORT")], 6) == -0.6
    feed.append(O(T + 30, "ETHUSDT", "BUY", "SHORT", 60, 1900.0, pnl=1))
    e.step()
    assert b.positions() == {}
    assert e.state["start_equity"] == 1234.5
