# Project Handover — 指数+期权双轨交易轮动系统

**最后更新**: 2026-04-29 (v3)  
**平台**: QuantConnect Cloud (Free 账户)  
**用户**: Du Guanyun (cloudmori86@gmail.com)  
**工作目录**: `D:\Claude_Code\QuantConnect\`

---

## 一、策略概述

**策略名**: 指数+期权双轨交易轮动系统（QQQ / TQQQ / QQQM）

| 标的 | 用途 |
|------|------|
| QQQ | 技术信号基准（200MA + RSI）+ 买入 LEAPS |
| TQQQ | 车轮策略核心（卖 CSP → 被行权 → 卖 CC → 被 Call 走 → 重启） |
| QQQM | 长期底仓（50%仓位，低费率） |

**开仓三重信号**（需同时满足）：
1. QQQ > 200日均线（牛市）
2. QQQ RSI < 50（短期买盘枯竭）
3. 当天收阴线

**防守信号**: QQQ 跌破 200MA → 立刻平仓或卖深度 ITM Call  
**抄底信号**: VIX ≥ 30 → 动用 10% 备用金加仓 QQQM

**仓位配比**（"年龄+20"法则，以 30 岁为例）：
- 50% QQQM 底仓
- 10% 现金备用（VIX ≥ 30 时动用）
- 32% TQQQ 车轮策略（CSP / CC）
- 8% QQQ LEAPS（Delta ~0.8，~2年到期，深度实值）

---

## 二、当前进度

### ✅ 已完成
1. **Research Notebook 开通** — 在 QuantConnect Algorithm Lab 项目 "Formal Black Alpaca" 内创建了 `research.ipynb`
2. **QQQ 期权数据抓取** — 2026-02-01 → 2026-03-16，25,066 行含 Greeks，已存入 `data/`
3. **TQQQ 期权数据抓取** — 2026-02-01 → 2026-04-17，13,204 行含 Greeks，已存入 `data/`
4. **TQQQ 2022 全年数据抓取** — 2022-01-03 → 2022-12-30，60,333 行含 Greeks，已存入 `data/`
5. **数据质量确认** — delta/IV 100% 非空；bid=ask 为 QC 历史回放限制，建仓用 `mid × 0.95` 估算滑点；Theta 为年化值需 ÷365
6. **期权复盘工具 V1** — `options_dashboard.py` Streamlit 看板，支持虚拟建仓/平仓/到期结算/权益曲线
7. **期权复盘工具 V2（备兑卖出）** — 新增 🛡️ 备兑卖出 Call 专区、`get_available_shares()`、Short Call ITM 备兑结算
8. **期权复盘工具 V3（正股直买 + 动态成本）** — 新增"🛒 买入100股"按钮、动态成本/股指标（公式：(买股支出-累计权利金)÷持股数）、重置账户按钮
9. **本地车轮策略闭环验证** — 跑通完整一轮：Short Put → 被行权 → 4轮 CC → 最终被 Call 走，净盈亏 +$237.50
10. **QC PnL 验证脚本（wheel_replay_qc.py）** — 2022 全年 TQQQ 车轮，11笔硬编码流水，验证 QC 现金 = $6,744
11. **QC 验证结论** — QC 回测因 Early Assignment（提前行权）× 2 次导致 200 股 × $45 = $9,000 支出（本地预期 $4,500），差异 $4,500；硬编码对照方案已放弃，改用本地 Streamlit 验证
12. **CCB 工具（ccb.py）** — Gemini × Claude Code 协作桥接工具，自动加载 HANDOVER.md 作为项目背景，用 `gemini-2.5-pro` 分析后将结果传递给 Claude Code
13. **Gemini 2022 数据分析** — 发现 3 个关键数据陷阱（见下方"数据陷阱"章节）

### 🔲 待完成
1. **options_dashboard.py 拆股复权** — 2022-01-13 TQQQ 2:1 拆股，跨越该日期的持仓 Strike ÷2、qty ×2、成本 ÷2
2. **main.py CC 防割肉逻辑** — `if CC_Strike < avg_cost: skip`，禁止在深套时贱卖 Call
3. **QuantConnect main.py 回测** — 在 Algorithm Lab 运行完整车轮策略回测（2021-2024）
4. **保证金冻结（V4）** — `account["collateral"]` 占位，未真正扣除 `strike × 100`
5. **QQQ 数据延伸** — 当前只到 2026-03-16，可重跑 `research_qqq_options.py` 补充到 2026-04-17

---

## 三、关键文件清单

| 文件 | 位置 | 说明 |
|------|------|------|
| `main.py` | QuantConnect Algorithm Lab | 完整车轮策略回测（2021-2024），待运行 |
| `wheel_replay_qc.py` | `D:\Claude_Code\QuantConnect\` | QC PnL 验证脚本（2022 全年 TQQQ，11笔流水） |
| `wheel_verify_notebook.py` | `D:\Claude_Code\QuantConnect\` | Research Notebook 版验证脚本（读 Object Store CSV） |
| `ccb.py` | `D:\Claude_Code\QuantConnect\` | Gemini × Claude Code 协作桥接工具 |
| `research.ipynb` | QuantConnect Research Notebook | Jupyter 数据探索环境 |
| `research_qqq_options.py` | `D:\Claude_Code\QuantConnect\` | 数据抓取脚本（当前配置为 TQQQ，2026-02-01→04-17） |
| `options_dashboard.py` | `D:\Claude_Code\QuantConnect\` | Streamlit 期权复盘工具 V3（含备兑卖出、正股直买、动态成本） |
| `data\QQQ_options_20260201_20260316_greeks.csv` | `data\` | QQQ 期权数据，25,066 行，含 Greeks |
| `data\TQQQ_options_20260201_20260417_greeks.csv` | `data\` | TQQQ 期权数据（2026），13,204 行，含 Greeks |
| `data\TQQQ_options_20220201_20221231_greeks.csv` | `data\` | TQQQ 期权数据（2022 全年），60,333 行，含 Greeks |
| `data\Simulator\2026-04-23T03-11_export.csv` | `data\Simulator\` | 2022 全年车轮流水导出（GBK 编码），11笔交易 |
| `gemini_analysis.md` | `D:\Claude_Code\QuantConnect\` | Gemini 2.5 Pro 对 2022 数据的分析报告 |

---

## 四、options_dashboard.py 功能说明

```bash
streamlit run options_dashboard.py
```

### 已实现功能
| 功能 | 说明 |
|------|------|
| QQQ / TQQQ 切换 | 侧边栏 radio，切换时自动清空持仓和账户 |
| 初始资金设置 | 默认 $10,000，可调，修改后重置账户 |
| 时间控制 | 侧边栏 slider + 主界面 ⏪-1日 / ⏭+1日 / ⏩+1周 / 📅跳到到期日 |
| 账户概览 | 初始资金 / 可用现金 / 已实现PnL / 未实现PnL / 总权益 |
| 股票持仓 | ITM 行权后显示正股持仓、成本价、市值、浮盈 |
| 期权链 | delta 蓝/红着色，含 Greeks 和 IV |
| 虚拟建仓 | Short/Long，自动更新账户现金；Short Call 有足量正股时自动标记备兑 |
| 🛡️ 备兑卖出专区 | 持股时显示；独立到期日选择器；可用股数/已锁定实时显示；张数上限自动计算 |
| 自动到期结算 | Short Put ITM → 买入正股；Short Call ITM 备兑 → 卖出正股收回现金；OTM → 权利金全额实现 |
| 合并权益曲线 | 所有持仓的权益随时间变化图 |
| 交易流水 | 可折叠，显示所有开仓/平仓/到期事件 |

### PnL 公式（重要）
```
pnl = direction × (cur_mid - entry_mid) × 100 × qty
# Short direction = -1：mid 下降 → PnL 为正（卖方获利）
# Long  direction = +1：mid 上升 → PnL 为正（买方获利）
```

### 到期结算逻辑
```python
# Short Put ITM（最常见车轮场景）
cash -= strike × 100 × qty          # 买入正股
shares[ticker] += 100 × qty          # 正股入账
realized_pnl += entry_mid × 100 × qty  # 权利金全额实现

# Short Put OTM
realized_pnl += entry_mid × 100 × qty  # 权利金全额实现，无现金变化

# ITM 内在价值用标的价推算，避免到期日 bid-ask 失真：
# Put value = max(0, strike - spot)
# Call value = max(0, spot - strike)
```

### 已知限制（V1）
- `bid = ask`：数据源限制，建仓请心算 `mid × 0.95` 作为实际成交价
- 无保证金冻结：裸卖 Put 不扣保证金，V2 补充 `strike × 100 × qty`
- 跨标的股票市价：持有 QQQ 股票时切换到 TQQQ 界面，股票显示成本价而非市价

---

## 五、QuantConnect 平台限制（Free 账户）

| 限制 | 说明 |
|------|------|
| 无 API 访问 | 不能用 REST API 读写 Object Store |
| 10KB 日志/天 | main.py 回测日志需精简 |
| Object Store | 可在 Notebook/Algorithm 内读写，无 UI 下载入口 |

**Object Store 键名**:
- `QQQ_options_20260201_20260316_greeks.csv`
- `TQQQ_options_20260201_20260417_greeks.csv`

---

## 六、`research_qqq_options.py` 关键逻辑

```python
# 当前配置（最后一次修改）
TICKER    = "TQQQ"
start_time = datetime(2026, 2, 1)
end_time   = datetime(2026, 4, 17)
STORE_KEY  = "TQQQ_options_20260201_20260417_greeks.csv"

# Greeks 获取方式（核心）
qb.SetDateTime(datetime(dt.year, dt.month, dt.day, 15, 45))
chain = qb.OptionChain(option.Symbol)   # OptionHistory() 不含 Greeks

# 车轮策略过滤
if not (7 <= dte <= 45): continue
if delta_abs < 0.05 or delta_abs > 0.60: continue
```

---

## 七、`main.py` 车轮策略回测说明

- **回测区间**: 2021-01-04 → 2024-12-31
- **状态机**: `SEEK_CSP` → `SHORT_PUT` → `LONG_TQQQ` → `SHORT_CALL` → 循环
- **CSP 参数**: Delta ~0.25，DTE 21-45
- **CC 参数**: 牛市 Delta ~0.30，熊市 Delta ~0.65（深度 ITM 保护）
- **LEAPS**: QQQ Call，Delta ~0.80，~2年到期
- **VIX 抄底**: `AddIndex("VIX")` — 触发 VIX ≥ 30 时加仓 QQQM
- **回测结果**: 存入 Object Store `wheel_trade_log.csv`

---

## 八、2022 TQQQ 数据陷阱（Gemini 2.5 Pro 分析，2026-04-29）

### ⚠️ 陷阱一：TQQQ 2:1 拆股（2022-01-13）
- ATM Call Strike：2022-01-13 = $141 → 2022-01-14 = $72.5（隔夜减半）
- **影响**：跨越该日期的持仓 Strike 必须 ÷2，qty ×2，正股成本 ÷2
- **待实现**：`options_dashboard.py` 预处理函数

### ⚠️ 陷阱二：Theta 为年化值（需 ÷365）
- 示例：Strike=$163 Call，mid=$8.375，theta=-84.35 → 实际日衰减 = -84.35/365 = **-$0.23/天**
- **影响**：直接使用 theta 列会高估衰减 365 倍
- **处理**：显示时 `daily_theta = theta / 365`

### ⚠️ 陷阱三：固定滑点在高 IV 时严重低估
- 2022 年 IV 均值 90%，峰值 120%+（10月暴跌期）
- `mid × 0.95` 在恐慌期严重低估真实价差
- **建议**：动态滑点 `fill = mid - mid × 0.02 × (iv / 0.5)`

### 📊 2022 数据概况
| 指标 | 数值 |
|------|------|
| 总行数 | 60,333 |
| 日期范围 | 2022-01-03 → 2022-12-30 |
| Strike 范围 | $6 → $265（TQQQ 年初 ~$65 → 年末 ~$17） |
| IV 均值/中位数 | 90% / 86.6% |
| bid=ask 比例 | 100%（QC 历史数据限制） |

---

## 九、下一步建议操作顺序

```
1. 拆股复权修复（紧急）
   → options_dashboard.py 加入 2022-01-13 复权预处理
   → 跨越拆股日的持仓 Strike ÷2，qty ×2，成本 ÷2

2. main.py CC 防割肉逻辑
   → if CC_Strike < avg_cost: skip（禁止在深套时贱卖 Call）
   → 200MA 以下时 Short Put Delta 从 0.25 降至 0.10~0.15

3. 运行 main.py 回测
   → QuantConnect Algorithm Lab → "Formal Black Alpaca" → Run Backtest
   → 查看 Logs / Charts / wheel_trade_log.csv

4. 保证金冻结（V4）
   → Short Put：cash_available -= strike × 100

5. CCB 工具使用
   → python ccb.py "你的问题"
   → 自动加载 HANDOVER.md 背景，Gemini 2.5 Pro 分析后传递给 Claude Code
```
