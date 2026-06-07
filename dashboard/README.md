# 实时交易分析看板（决策支持，非自动交易）

NiceGUI 看板：黄金、原油、外汇主要货币对的实时分析。识别最明显的可买/可卖趋势，
给出确定性指标 + 多智能体 LLM 解读 + 新闻情绪。**只出建议，绝不自动下单。**

## 运行

```bash
cd C:\Users\ls\Desktop\Claude\quant
python -m dashboard.app          # 然后浏览器打开 http://localhost:8080
```

或从项目根目录：`python run_dashboard.py`

## 界面

- **顶部**：自动刷新选择器（1 / 10 / 15 / 30 / 60 分钟，默认 **15**）、周末暂停 LLM 开关、
  手动刷新按钮、**今日 API 调用计数 (x/200)**、各层最近刷新时间。
- **Macro backdrop**：LLM 综合的宏观/风险背景。
- **Top Opportunities**：按"明显程度"排序的最强可买/可卖趋势。
- **All instruments**：每个品种一张卡（价格 + 确定性结论 + LLM 解读 + 失效价位），
  点 Details 看完整事实。颜色：绿=BUY，红=SELL，灰=WAIT/WATCH。

## 两层刷新（为省 API 额度而设计）

- **便宜层**（价格 + 确定性趋势打分）：按选择的间隔刷新，**0 次 LLM 调用**。可以每分钟刷。
- **LLM 板扫描**：**一次调用分析全部品种**（批量），最短间隔 10 分钟，且受预算守卫保护。
  → 默认 15 分钟约 96 次/天，远低于 200 上限，留足手动深挖的余量。

**预算守卫**：每日调用数持久化在 SQLite（重启不清零）。接近上限（200-10）时自动停掉
LLM 调用，看板继续用确定性数据更新，绝不会因超额被掐断。

**周末暂停**：勾选后，周六日不自动跑 LLM（外汇休市）。手动刷新会**无视**该暂停。

## 数据源

- **价格**：优先 MT5（若本机终端在运行，改 `instruments.py` 里的 mt5 符号名匹配你的经纪商），
  否则自动回退 **yfinance**（免费，当前默认）。
- **新闻**：Finnhub（在 `analyst/.env` 设 `FINNHUB_API_KEY` 后启用）+ RSS 聚合兜底。
- **LLM**：复用 `analyst/` 的 OpenAI 配置（gpt-5-mini via 代理）。

## 接入 MT5 近实时（near-tick）

价格层优先用 MT5，没有终端时自动回退 yfinance（延迟约 15 分钟、小时线）。
价格卡会标 `● live`（MT5 tick）或 `○ delayed`（yfinance）。代码在 `mt5_client.py`，
没装/没登录 MT5 时所有调用安全返回 None，不影响其余功能。

**配置步骤：**
1. 安装 MT5 终端，登录账户（demo 即可），开启"算法交易"。
2. `uv sync --extra mt5` 安装 MetaTrader5 包。
3. 发现你经纪商的 Gold/Oil 符号名（各家不同）：
   ```powershell
   uv run python -m dashboard.mt5_client
   ```
   把打印出的正确符号名填到 `instruments.py` 的 `mt5` 字段（注意后缀如 `.r`/`-ECN`）。
4. （可选）在 `analyst/.env` 配置自动登录：`MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` / `MT5_PATH`。
   不配则附着到已运行的终端。

**它如何取数：**
- 当前价（近实时）：轮询 `symbol_info_tick` → bid/ask/mid + 真实点差（每次 cheap 刷新取一次）。
- 分析历史：`copy_rates_from_pos`（H1）。
- **精确 SL/TP 判定**：`copy_ticks_range` 拿成交窗口内每个 tick，按时间顺序判断
  SL 与 TP **谁先被打到**——去掉了 yfinance 路径下"假定先打 SL"的保守假设，更准。

注意：MT5 时间是经纪商服务器时区（UTC 基准），代码统一转 UTC；连接非线程安全，已加锁串行化。

## 本机 TLS 说明

AVG 杀毒拦截 HTTPS 并用本地根证书重签。`net.py` 一次性解决两层：truststore 走 Windows
信任库（修 httpx/openai），并把 Windows 根证书导出成 `winca.pem` 供 curl_cffi/requests/
yfinance 使用。**全程开启校验，无任何降级。**

## 模拟交易 / 前向跟踪（Paper Trades）

为每个合格信号自动生成 SL/TP 设置，用真实后续价格判定胜负，**诚实地**统计成绩
（首要指标是每笔的 R 期望值 expectancy，其次才是胜率）。

**下单触发的门槛（漏斗，逐级筛）**：方向≠WAIT → 确定性趋势与动作一致(confluence)
→ LLM 置信度≥0.60 → 趋势强度≥3/5 → 通过风控（有效 ATR 止损）→ R:R≥1.5 → 同品种无未平仓。
每个被拒的信号都记录原因，不静默跳过。

**两种 SL/TP 并行对比**：
- `ATR`：SL=2×ATR，TP 按固定 R:R（默认 2:1）。
- `STRUCT`：SL=失效位/最近支撑阻力，TP=对侧结构，R:R 由结构决定（<1.5 则跳过）。

**判定规则（防自欺）**：成本（半点差）进出各收一次；同一根 K 线同时触及 SL 和 TP 时
**假定先打到 SL**（保守）；5 个交易日内未触及则到期按市价平仓记部分 R。

**历史回放（加速器）**：`python -m dashboard.replay --period 5y` —— 用**仅确定性**信号
在过去数据上生成并判定交易，立即得到成绩（LLM 不回放，避免未来函数；LLM 只能靠实时前向验证）。
近期 5y 结果：确定性趋势信号扣成本后基本盈亏平衡（期望值 ~0），R:R 越高略好。

界面底部 "Paper Trades — Forward Track Record"：按方法分组的 expectancy/胜率/PF 卡片
（n<30 标注"不可信"）、未平仓表、已平仓结果表。"Log trades now" 按钮可手动按当前信号下单。

## 文件

```
net.py          TLS 引导 + 加载 .env + 把 quant/ 加入 path（必须最先 import）
instruments.py  品种清单（黄金/原油/外汇 + 各数据源符号映射）
providers.py    取价（MT5 优先, yfinance 回退）
scoring.py      确定性趋势打分 + 排序（免费的"明显趋势"识别器）
news_sources.py Finnhub + RSS 新闻聚合
board_scan.py   批量单次 LLM 调用（预算守卫）
store.py        SQLite：每日预算计数 + 缓存
service.py      内存实时状态 + 两层刷新函数
app.py          NiceGUI 界面
```
