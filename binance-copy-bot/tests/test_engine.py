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
    e = Engine({"capital_usdt": 1000, **cfg}, lead, b, msgs.append, persist=False)
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
    e = Engine({"capital_usdt": 1000}, s, b, persist=False)
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
