# 抗自欺回测框架 (anti-self-deception backtester)

这套框架的目的不是让你看到漂亮的资金曲线，而是**尽快、尽狠地告诉你一个交易想法是不是假的**。
它把散户量化最容易骗自己的三个地方做成了硬约束，你想作弊都难。

## 它如何防止你骗自己

| 自欺方式 | 框架的防御 |
|---|---|
| **未来函数**（用还没发生的信息） | 引擎内部强制 `signal.shift(1)`：t 收盘算出的信号，只能从 t+1 开始持仓。无法关闭。见 `engine.py`。 |
| **零成本回测** | `CostModel` 必填点差/滑点/手续费，设为 0 会警告。默认值 `RETAIL_FX_MAJOR` 故意偏悲观。见 `costs.py`。 |
| **样本内过拟合** | 只认 **walk-forward**：参数只在训练窗口优化，业绩只取紧随其后、从未参与优化的样本外窗口拼接而成。见 `walkforward.py`。 |
| **疯狂调参后挑最好的** | **Deflated Sharpe Ratio**：你试了多少组参数，就按概率扣多少分。DSR < 95% = 别信。见 `metrics.py`。 |
| **整个流程本身有 bug** | **噪声测试**：把同一套流程跑在零漂移随机游走（无 edge 的市场）上。如果它"赚钱"，说明你有未来函数或在挖噪声。见 `run_noise_test.py`。 |

## 快速开始

```bash
pip install -r requirements.txt

# 1) 先验证框架本身诚实（最重要的一步，先做这个）
python run_noise_test.py --trials 40 --strategy ma_crossover
#   期望: mean OOS Sharpe ~0 或略负(成本)，false 'edges' ≈ 0% → PASS

# 2) 在玩具市场上跑完整 walk-forward
python run_demo.py --strategy ma_crossover

# 3) 换成你自己的数据
python run_demo.py --strategy breakout --csv your_data.csv
```

用你自己的 MT5 数据（需在本机运行 MT5 终端）：

```python
from data import load_mt5
prices = load_mt5("EURUSD", "H1", n=50000)
```

## 怎么读结果

只看一个东西：**OOS（样本外）DEFLATED Sharpe**。

- **DSR ≥ 95% 且 OOS Sharpe > 0** → 也许有 edge。即便如此，下一步是**模拟盘实时跑**，不是直接上钱。
- **OOS Sharpe ≤ 0** → 扣完成本没有 edge，丢掉这个想法。
- **OOS Sharpe > 0 但 DSR < 95%** → 最常见的情况：正收益只是调参调出来的运气。**不要交易。**

还要看每个 fold 的 `is_sharpe` vs `oos_sharpe`：样本内高、样本外塌 = 过拟合的铁证。

## 文件结构

```
costs.py          成本模型（强制存在）
engine.py         向量化引擎（结构上杜绝未来函数）+ 年化推断
metrics.py        业绩指标 + 抗过拟合指标 (DSR, PSR)
data.py           合成 GBM / CSV / MT5 三种数据源
strategies.py     示例策略 + 参数网格（网格大小=试错次数，会被 DSR 惩罚）
walkforward.py    walk-forward harness（唯一可信的回测）
run_demo.py       端到端示例
run_noise_test.py 照妖镜：在噪声上验证框架本身
```

## 加你自己的策略

在 `strategies.py` 写一个函数 `(prices, **params) -> signal`，signal ∈ {-1,0,+1}，
其中 `signal[t]` 是用截至 t 收盘的信息做出的决定。**不要自己 shift**，引擎会处理 t+1 执行。
然后注册到 `STRATEGIES`，附上参数网格。

## 重要前提

这个框架能告诉你一个 edge 是真是假，**但它不会替你创造 edge**。
对绝大多数散户，外汇里持续的 edge 极难找到。这个工具的最大价值，是让你在亏真钱之前
就快速否决掉 99% 行不通的想法——以及，诚实地确认你到底有没有那 1%。
