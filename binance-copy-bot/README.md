# 币安带单员影子跟单机器人

轮询某个带单员的**公开成交记录**，按「你的本金 ÷ 他的参考本金」的固定比例，在你自己的币安 U 本位合约账户里同步开仓、加仓、减仓、平仓。

> 重要：读取带单员数据用的是币安网页的内部接口，不是官方 API，可能失效、限流或违反用户条款。
> 熬鹰资本隐藏了「持有仓位」，所以本项目用「最新交易记录」还原他的仓位。读取接口已实测可用；
> 下单部分（官方合约 API）尚未用真实账户测试过，实盘前务必先模拟、再小资金。

## 它做什么 / 不做什么

- 他开/加/减/平仓 → 你按比例跟。他平仓（无论止盈还是止损）你就平仓。
- 加仓时只有新增部分按当时的比例跟；减仓时按他减掉的百分比同比例减；他的余额变化不会让你已有的仓位被动加减。
- 看不到他的止盈止损挂单，所以不会替你预挂止盈止损单。
- 有几秒延迟，成交价会和他不同。
- **只跟机器人启动之后他新开的仓位**。启动前他已有的仓位看不到，不跟；他给老仓加仓时，只跟加的那部分。
- 成交记录的公开延迟未知。模拟运行时日志会打印 `lag=N s`（发现延迟 N 秒），延迟很大就不适合实盘。
- 他会交易 TradFi 品种（如 NVDAUSDT）。你的账户若没开通这类合约，对应的单会下单失败并提示。

## 安装

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env
```

## 使用顺序（不要跳步）

1. `config.yaml` 里 `portfolio_id` 已填好熬鹰资本，按需改资金参数。
2. `python -m copybot check` —— 能打印出他最近 10 笔成交，且「合约行情接口」显示正常，说明网络可用。
3. `python -m copybot run` —— 默认 `dry_run: true`，只打印「[模拟] 开/加仓 …」，不下单。跑 3–7 天观察。
4. 实盘：`.env` 填 API Key，`config.yaml` 改 `dry_run: false`，先用很小的 `capital_usdt`。

## 资金与亏损设置（config.yaml）

| 参数 | 含义 |
|---|---|
| `capital_usdt` | 你投入的本金，决定跟单比例 |
| `max_loss_usdt` | 累计亏损到此值：全部平仓并停止，需 `python -m copybot reset` 才恢复 |
| `daily_max_loss_usdt` | 当日（UTC）亏损到此值：全部平仓，当天不再开仓 |
| `max_leverage` | 你的杠杆上限 |
| `max_notional_per_symbol` / `max_total_notional` | 单币种 / 总名义仓位上限 |
| `max_entry_slippage_pct` | 现价比他的均价差太多就不追 |

亏损是按**整个合约账户权益**的变化计算的，所以：

- 建议用一个**专用子账户**跑，里面只放跟单本金；
- 运行期间不要在同一账户手动交易同一币种，也不要充提，否则亏损统计和仓位对账会被干扰。

本金太小时，按比例算出的数量可能低于交易所最小下单量，这些单会被跳过（日志里有提示）。

## API Key 安全

- 只勾选「允许合约」，**不要勾选提现**。
- 绑定运行机器的 IP 白名单。
- Key 只写在 `.env` 里，不要发给任何人，不要提交到 Git（已在 `.gitignore`）。

## 24 小时运行

机器人停了就不跟单，也**不会**替你平仓（已有仓位会留在账户里），所以需要一台不关机的机器：

- 云服务器（推荐）：任意 1 核 1G 的 Linux VPS 即可。
  ```bash
  docker build -t copybot .
  docker run -d --name copybot --restart unless-stopped \
    -v $PWD/config.yaml:/app/config.yaml -v $PWD/.env:/app/.env -v $PWD/data:/app/data copybot
  ```
  （用 Docker 时把 `state_file` 改为 `data/state.json`、`lead_state_file` 改为 `data/lead_state.json`）
- 自己的电脑：能跑，但休眠、断网、重启都会中断跟单。

## 测试

```bash
pip install pytest && python -m pytest -q tests
```
