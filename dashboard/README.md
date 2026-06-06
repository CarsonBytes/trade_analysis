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

## 本机 TLS 说明

AVG 杀毒拦截 HTTPS 并用本地根证书重签。`net.py` 一次性解决两层：truststore 走 Windows
信任库（修 httpx/openai），并把 Windows 根证书导出成 `winca.pem` 供 curl_cffi/requests/
yfinance 使用。**全程开启校验，无任何降级。**

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
