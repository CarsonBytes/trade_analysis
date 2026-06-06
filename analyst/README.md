# 多智能体行情分析（决策支持，非自动交易）

这是你最初提案的**诚实版**：一个 LangGraph 多智能体系统，在你做交易决定**之前**帮你
把信息收集、组织、并由多个分析师 agent 辩论，最后给你一份简报。

**核心设计原则**：LLM 永远不计算数字。代码算指标（RSI/ATR/趋势/支撑阻力），
agent 只**对这些事实进行推理**。这样分析可复现、可审计，模型也无法瞎编指标值。
风控是确定性规则，拥有最终否决权——钱的事不能交给模型的心情。

## 数据流

```
确定性事实 (features.py: 指标/区间/波动率)
      │
      ├──► Regime Agent     判断市场状态 (趋势/震荡/高波动)   ┐
      ├──► Technical Agent  多周期方向 + 强度                 ├─ LLM, 并行
      ├──► Sentiment Agent  新闻 → 情绪分(-10..+10) + 关键事件 ┘
      │
      └──► Decision Agent   (LLM: 综合 → BUY/SELL/WAIT + 置信度 + 失效条件)
               │
               └──► Risk Gate (确定性: 仓位计算 + 事件黑名单, 可否决 → WAIT)
                        │
                        └──► 给你的交易简报
```

## 运行

```bash
pip install -r ../requirements.txt          # 含 langgraph/langchain-openai/truststore
# 配置: 复制 .env.example -> .env, 填 OPENAI_API_KEY (可选 OPENAI_BASE_URL / OPENAI_MODEL)

# 从 quant/ 目录运行 (注意是 -m 模块方式)
cd ..
python -m analyst.run --csv eurusd_daily.csv --symbol EURUSD
python -m analyst.run --mt5 EURUSD --tf H1               # 实时从 MT5 拉数据
python -m analyst.run --csv eurusd_daily.csv --no-news   # 跳过新闻抓取
```

参数：`--equity`（账户净值，默认 10000）、`--risk`（每笔风险占比，默认 0.005=0.5%）。

## 输出怎么读

简报分四段：**FACTS**（代码算的硬事实）、**ANALYST VIEWS**（三个 agent 的结构化意见）、
**DECISION**（主交易员 agent 的综合判断 + 失效价位 + 分歧记录）、
**RISK GATE**（确定性风控，可能把 LLM 的决定否决成 WAIT 并标 `*** VETOED LLM ***`）。

**WAIT 是合法且经常正确的答案。** 系统倾向于在信号冲突时按兵不动，而不是硬凑一笔交易。

## 关于这台机器的 TLS

本机装了 AVG 杀毒，它会拦截 HTTPS 并用自己的本地根证书重签——这个根被 Windows 信任、
但不在 certifi 里。所以 `llm.py` 用 `truststore` 走 **Windows 系统信任库**校验。
TLS 校验全程开启，没有任何降级。

## 诚实的边界（务必记住）

- 这套系统的价值是**结构化的研究/简报助手**：帮你不漏看信息、强迫每个角度都表态、
  记录分歧、给出明确的失效条件。它让你的主观决策更有纪律。
- 它**不是 alpha 来源**。多个 LLM 投票不会凭空产生统计优势；新闻在你读到时早已被定价。
- 真正的判断、真正的下单，由**你**来做。系统只出建议，绝不下单。
- 想验证某个想法到底有没有 edge，用隔壁的回测框架（`../README.md`），不是用这个。

## 加新 agent / 改逻辑

- 新分析师：在 `state.py` 加一个 pydantic 输出模型，在 `nodes.py` 写节点函数，
  在 `graph.py` 里接进 fan-out/fan-in。
- 改风控：`nodes.py` 的 `risk_gate_node`，纯 Python 规则，是最终权威。
- 换模型/供应商：只改 `llm.py` 一个文件。
```
