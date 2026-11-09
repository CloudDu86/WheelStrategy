# Project Handover — 指数+期权双轨交易轮动系统

**最后更新**: 2026-11-09（**v6+** — QQQ 2012–2026 数据补全〔下载方法见第十二章〕+ `auto_wheel.py` 自动车轮回测；此前 v6：MooMoo 交易成本模型 + 部分平仓 + 存盘 + GLD/QQQ/TQQQ 短期数据按年抓取）
**平台**: QuantConnect Cloud (Free 账户) + 本地 Streamlit Dashboard
**用户**: Du Guanyun (cloudmori86@gmail.com)
**工作目录**: `D:\Coding_Projects\QuantConnect\`（旧路径 `D:\Claude_Code\QuantConnect\` 已弃用；抓取脚本注释里仍写旧路径）
**GitHub**: https://github.com/CloudDu86/WheelStrategy

---

## 一、策略概述

**策略名**: 指数+期权双轨交易轮动系统（QQQ / TQQQ / QQQM）+ VOO Covered Call 收租 + QQQ LEAPS 多头加速

| 标的 | 用途 |
|------|------|
| QQQ | 技术信号基准（200MA + RSI）+ **★ 买入 LEAPS（深 ITM Long Call Δ≈0.80）** |
| TQQQ | 车轮策略核心（卖 CSP → 被行权 → 卖 CC → 被 Call 走 → 重启） |
| QQQM | 长期底仓（50%仓位，低费率） |
| **VOO** | **实盘 SBI 持仓 159 股 @ ~$430**，目标用 CC 月度收租 |

**开仓三重信号**（需同时满足）：
1. QQQ > 200日均线（牛市）
2. QQQ RSI < 50（短期买盘枯竭）
3. 当天收阴线

**防守信号**: QQQ 跌破 200MA → 立刻平仓或卖深度 ITM Call
**抄底信号**: VIX ≥ 30 → 动用 10% 备用金加仓 QQQM

**仓位配比修订建议**（基于 v5 日本税务分析，30 岁，¥1000 万级别）：
- 45% 株（VOO/eMAXIS S&P500 — 比 Gemini 建议多保 5% 避税）
- 13% 现金（比 HANDOVER 原 10% +3%，保证 TQQQ 全行权能接货）
- 12% TQQQ 车轮（起步档，半年验证后再加到 18-20%）
- 10% 金 / 10% 债与高配当
- 5% QQQ LEAPS（Δ≈0.80，2 年到期）
- 2.5% SOXL / 2.5% LABU（高波突击队，定期再平衡）

---

## 二、当前进度

### ✅ v6 完成项（commit `230c20c`，2026-06-16）

**Dashboard（options_dashboard.py，~1950 行）**

1. **MooMoo Japan 交易成本模型**（按 2026-06-02/03 实盘成交确认书校准）
   - 手续费**只在新规（开仓）收取**：每单固定 `$2.07` + 每张 `$0.24`（1 张 ≈ $2.31，实测报告 $2.30 ✓）
   - **返済（平仓）免费** → 1 张完整往返 ≈ $2.30，不是 $4.60
   - 佣金：活动期（≤7 张）$0；活动结束后 0.33/张（税込）→ 侧边栏手动改
   - 滑点默认 `$0.015/股`，开、平两腿都计入成交价（买入往上、卖出往下）
   - 到期作废 / 被行权结算**不收费**
   - 侧边栏「💱 交易成本」面板，可整体开关；相关函数 `_txn_costs()` / `_fee_for(qty)`
   - 开仓手续费在开仓时一次记账，并带入该仓位的 realized PnL
2. **部分平仓** `partial_close_position()`：每个仓位可输入平仓张数，拆成「已平部分」+「剩余仓位」，开仓手续费按张数比例分摊
3. **存盘到本地**：「保存为新文件 / 覆盖源文件」直接写入 `data/Simulator/`（覆盖只在加载过本地 workspace 后可用）
4. **到期日下拉框跨日期步进保持选中**（key + session 镜像）
5. **+1日/+1周 按钮自动改名**：LEAPS 这类周度采样数据时按钮语义随之调整
6. **Short 仓位也启用 intrinsic fallback**：报价缺失（如拆股导致行权价断档）时按内在价值估算（v5 只对 Long 生效）

**数据抓取脚本（按年分文件，规避 50MB Object Store 上限 + 免费账户长时间运行被终止）**

7. `research_gld_options.py` — GLD DTE 0-60，Δ 0.02-0.60，每日采样，2022-2025 单文件（用于 GLD 卖 Call 收租 / 0DTE 测试）
8. `research_qqq_shortcall.py` — QQQ DTE 1-60，Δ 0.05-0.70，每日 15:45 ET 快照，2021-2025 每年一个 CSV（改 `YEAR` 逐年跑）
9. `TQQQ_trades_summary.txt` — TQQQ 38 笔交易按时间顺序导出 + 分类统计（胜率 37/38）

### 🆕 v6 之后（未提交）

10. `research_tqqq_calls.py` — **研究「买 TQQQ Call vs 买 QQQ Call」**。TQQQ DTE 1-60，Δ 0.05-0.95，每日采样，2021-2025 每年一个 CSV。
    - 修正旧 TQQQ 数据的盲区：旧数据 Δ ≤ 0.60 / DTE ≥ 7，赚钱的 Call 进 ITM 后、以及临近到期的平仓都会掉出数据集
    - 注意 2022-01-13 TQQQ 2:1 拆股：Raw 模式下 2021 年的行权价约为拆股后的 2 倍
    - 5 个年度 CSV 已在 `data/`；旧的 2022/2023 TQQQ 数据已移入 `data/Old data/`
11. `workspace_QQQ_Call_option_2026-06-14.json` — QQQ 短期买 Call 复盘存档
12. `workspace_GLD_2026-06-03.json` / `workspace_QQQLeaps_2026-06-03.json` — GLD 卖 Call、QQQ LEAPS 复盘进度

### 🆕 2026-11-09：QQQ 2012–2026 数据补全 + 自动车轮回测（未提交）

13. **`auto_wheel.py`** — QQQ 自动车轮回测器，按用户交易逻辑自动交易：
    - Sell Put（DTE≈14、Δ≈0.20）→ 被行权后卖 Covered Call（Δ≈0.30，行权价永不低于动态成本；低于成本则卖 ≥ 动态成本的最低行权价；权利金 < $0.15 视为深套等待）
    - 直接买入：日线 W 底突破颈线 + 低点距 52 周高 ≥15% + 落在周线支撑区 ±3%
    - **仓位按权益定**：`put_alloc=0.8`（新开 Put 后 持股成本+担保金 ≤ 80% 权益），直接买入每次约 `direct_alloc=0.2` 权益
    - 早年只有月度期权时自动退到 ≤45 天的最近到期日
    - `--years 3` 固定 3 年随机期间，输出第 1/2/3 年末的回撤、持仓、动态成本、敞口、深套天数；每段生成可在 Dashboard 加载的 workspace JSON（`data/Simulator/auto_wheel/`）
    - 数据在 `data/` 或 `data/<TICKER>/` 下都能找到
    - 测试：`tests/test_auto_wheel.py`
14. **QQQ 期权 2012–2020 + 2026 年数据**已下载（下载方法见 **第十二章**），连同 2021–2025 共覆盖 2012-01 → 2026-11；QC 上 2011 年及以前没有 QQQ 期权数据
15. **`QQQ_daily_20050101_20261106.csv`** — QQQ 未复权日线（信号/到期结算/预热用）
16. `research_qqq_options_history.py` — 按年抓取模板（与 `research_qqq_shortcall.py` 同口径 Δ 0.05–0.70）

### ✅ v5 完成项

**Dashboard 功能（options_dashboard.py，~1700 行）**

`数据层`
1. ★ **文件扫描器扩展**：接受 `_options_` 和 `_LEAPS_` 两类命名，下拉显示 `[options]/[LEAPS]` 前缀
2. ★ **intrinsic value fallback (`get_mid_smart`)**：合约报价缺失时（如 LEAPS DTE 跌出过滤范围）自动用 `max(spot-strike, 0)` 估算 Long 仓位当前价。Short 不估算（不安全）。
3. ★ **批次独立成本追踪 (lot-level tracking)**：每次买入/被行权追加一个 `lot` 记录，含 qty/price/date。Short Call 行权时按 **FIFO** 消耗最早批次。
4. ★ **股票持仓 UI**：从加权平均成本表改为**每批次独立一行**显示

`期权链层`
5. ★ **🚀 LEAPS 模式**侧边栏开关 — 自动过滤 Call + DTE>365 + Delta 0.6-0.9
6. ★ **Delta 甜区高亮**：链表中 Δ 绝对值 0.75-0.85 行绿色加粗
7. ★ **Delta < 0.10 软警告**：远 OTM 价格易失真（多个行权价 mid 相同）→ 橙色背景
8. ★ **一键买入最优 LEAPS**：自动按 Δ≈0.80 选合约 + 限价支付

`时间控制`
9. ★ **⏰ Time Machine**：回放历史到任意日期，重建账户/持仓/批次/权利金状态（基于 history 重放，端点精确对齐）。支持「返回现在」一键恢复。
10. ★ **「跳到期日」改造**：到期日不在快照范围内时自动取最近 ≤ 到期日的快照
11. ★ **「跳到期日 1 周之前」按钮**：辅助 Roll Up & Out 收租时机检视

`持仓 UI`
12. ★ **「当前持仓」移到顶部**：放在「股票持仓」下方、期权链之前，开 LEAPS 仓位后**滚动一屏内可见**
13. ★ **平仓支持 intrinsic fallback**：无报价时按 intrinsic 价平仓（提示 ⋯ 标记）
14. ★ **Roll Up & Out 预览实时刷新**：去掉 `st.form` 包裹，切换新合约时立即重算 net credit（之前因 form 不重渲染显示错误金额）

**数据抓取脚本**
15. ★ **`research_qqq_leaps.py`** — QQQ LEAPS 8 年数据脚本（2018-2025，DTE 30-900，Delta 0.50-0.95，仅 Call）
16. ★ **`research_voo_leaps.py`** — VOO LEAPS 备用脚本（未跑）
17. ★ **纯 ASCII 设计**：避免 QC Notebook API 拒绝 emoji 导致无法保存
18. ★ **ObjectStore Save 软失败**：QC Free 50MB 上限超时不阻断，Cell 5 仍可下载

**新数据集**
19. ★ **QQQ_LEAPS_20180101_20251231_greeks.csv** — 37.45 MB / 326,624 行 / 8 年覆盖

### 🔲 后续待办（v5 遗留 + v6 新增）

0. ★ **TQQQ Call vs QQQ Call 对比复盘** — 数据已齐（两边 2021-2025 年度 CSV），在 Dashboard 里用同一信号日对比；记得把 `research_tqqq_calls.py` 提交进 git
0b. ★ **清理重名文件** `data/TQQQ_options_20230101_20231231_greeks (1).csv` → 去掉 ` (1)`，否则 Dashboard 文件扫描器可能识别异常
1. ~~**VOO 4 年 CC 模拟跑完**~~ → 已完成（`workspace_VOO_2023_2026.json`）
2. **TQQQ 拆股复权** — 2022-01-13 TQQQ 2:1 拆股，跨日持仓 Strike ÷2、qty ×2
3. **main.py CC 防割肉逻辑** — `if CC_Strike < avg_cost: skip`
4. **QuantConnect main.py 回测**（2021-2024）
5. **保证金冻结 V5** — `collateral` 改为真实 `strike × 100` 扣减
6. **VOO LEAPS 数据抓取**（可选）— `research_voo_leaps.py` 已就绪，未执行
7. **Δ > 0.95 LEAPS 边界数据**（可选）— 当前 LEAPS 数据 Δ 上限 0.95，超深 ITM 会丢；intrinsic fallback 已可应对，但若需精确价格需重抓时把上限改 0.99
8. **Roll Up & Out 收租实测**（VOO 4 年场景），目标永不被 Call 走

---

## 三、关键文件清单

### 代码文件（项目根目录）

| 文件 | 大小 | 说明 |
|------|------|------|
| `options_dashboard.py` | ~85 KB | **Streamlit 复盘工具 v6**（~1950 行） |
| `trade_chart.py` | 10 KB | **★ 交易复盘图**（Dashboard「📊 交易复盘图」区块）：Put-Call Parity 反推标的价格 + 每笔交易的行权价区间/开平仓点 + 单笔合约 mid/Δ。依赖 `plotly`；测试 `python -m pytest tests` |
| `research_tqqq_calls.py` | 8 KB | **★ v6 后** TQQQ DTE 1-60 / Δ 0.05-0.95，按年 |
| `research_qqq_shortcall.py` | 8 KB | **★ v6** QQQ DTE 1-60 / Δ 0.05-0.70，按年 |
| `research_gld_options.py` | 8 KB | **★ v6** GLD DTE 0-60 / Δ 0.02-0.60，2022-2025 |
| `TQQQ_trades_summary.txt` | 6 KB | **★ v6** TQQQ 38 笔交易汇总 |
| `research_qqq_options.py` | 8 KB | 短期期权抓取脚本（VOO 模板，DTE 1-60） |
| `research_qqq_leaps.py` | 11 KB | **★ QQQ LEAPS 抓取脚本 v2**（DTE 30-900，纯 ASCII） |
| `research_voo_leaps.py` | 11 KB | VOO LEAPS 抓取脚本（备用，未跑） |
| `csv_to_workspace.py` | 5 KB | CSV 流水 → JSON workspace 转换器 |
| `main.py` | 22 KB | QC Algorithm 完整车轮回测（未运行） |
| `wheel_replay_qc.py` | 8 KB | QC PnL 验证 notebook 脚本 |
| `wheel_verify_notebook.py` | 9 KB | QC 验证脚本 |
| `ccb.py` | 15 KB | Gemini × Claude Code 协作桥 |

### 数据文件（data/）

| 文件 | 大小 | 行数 | 说明 |
|------|------|------|------|
| **QQQ_LEAPS_20180101_20251231_greeks.csv** | **37.45 MB** | **326,624** | **★ QQQ LEAPS 8 年 (DTE 30-900, Δ 0.50-0.95, Call only)** |
| VOO_options_20220101_20251231_greeks.csv | 14.28 MB | 133,562 | VOO 4 年（2022-2025），DTE 7-60 |
| **GLD_options_20220101_20251231_greeks.csv** | 49 MB | — | ★ v6 GLD 4 年，DTE 0-60，每日 |
| **QQQ_options_\<YEAR\>0101_\<YEAR\>1231_greeks.csv** ×15 | 1.5-40 MB/年 | — | ★ QQQ 2012-2026（2021-2025 为 v6；2012-2020、2026 为 2026-11-09 补抓），DTE 1-60，Δ 0.05-0.70，每日 15:45 快照；2026 截至 11-06。现放在 `data/QQQ/` |
| **QQQ_daily_20050101_20261106.csv** | 0.26 MB | 5,497 | ★ QQQ 未复权日线（`data/QQQ/`） |
| **TQQQ_options_\<YEAR\>0101_\<YEAR\>1231_greeks.csv** ×5 | 10-20 MB/年 | — | ★ v6 后 TQQQ 2021-2025，DTE 1-60，Δ 0.05-0.95，每日（2023 文件名带 ` (1)` 待改） |
| `Old data/` | — | — | 旧 TQQQ 2022/2023 数据（DTE 7-45，Δ 0.05-0.60），已被新数据替代 |

> 数据 CSV 不进 git，只在本地 `data/` 下。

### Workspace JSON (data/Simulator/)

| 文件 | 说明 |
|------|------|
| `workspace_VOO_initial.json` | VOO 模拟起始状态（159 股 @ $430，现金 $31,630） |
| `workspace_VOO_2026-05-15.json` | VOO 复盘进展（5 笔 closed） |
| `workspace_VOO_2023_2026.json` | VOO 完整 4 年复盘存档（38 笔 closed, +$14,385，胜率 86.8%） |
| `workspace_TQQQ_20231215.json` | TQQQ 2 年完整 Wheel 存档（+43.9%） |
| `workspace_TQQQ_2026-05-02.json` | TQQQ 2026 测试 |
| `workspace_QQQLeaps_2026-06-03.json` | QQQ LEAPS 复盘进度（取代 05-21 起始版） |
| `workspace_GLD_2026-06-03.json` | **★ v6** GLD 卖 Call 复盘 |
| `workspace_QQQ_Call_option_2026-06-14.json` | **★ v6** QQQ 短期买 Call 复盘 |
| `workspace_TQQQ_20230721.json` | TQQQ 中间快照（18 笔，是 20231215 版的子集） |

---

## 四、options_dashboard.py v5 功能详解

```bash
streamlit run options_dashboard.py
```

### 4.1 数据加载（扩展为支持 LEAPS）

文件命名规则：
- `TICKER_options_<period>_greeks.csv` — 短期 CC/CSP 数据
- `TICKER_LEAPS_<period>_greeks.csv` — 深 ITM 长期 LEAPS 数据

下拉显示：`[options] 20220101 → 20251231` / `[LEAPS] 20180101 → 20251231`

### 4.2 主区段顺序（v5 重排）

```
账户概览
   ↓
股票持仓（lot 级别表，每批次独立一行）
   ↓
当前持仓（v5: 移到这里，包括平仓/Roll Up/利润锁定 操作）
   ↓
期权链 (LEAPS 模式开启时自动过滤 Call + DTE>365 + Δ 0.6-0.9)
   ↓
🚀 一键买入最优 LEAPS（仅 LEAPS 模式可见）
   ↓
🛡️ 备兑卖出 Call（持有股票时自动出现，与 LEAPS 模式互不冲突）
   ↓
虚拟建仓
   ↓
总权益曲线
```

### 4.3 ⏰ Time Machine（v5 新增）

```python
def rewind_to_date(initial_capital, history, positions, closed_positions, target_date):
    """从 history 重放到 target_date，端点精确还原账户+仓位+批次状态"""
```

- 侧边栏「⏰ 时间机器」区块，date 选择 + 「回到该日」/「返回现在」按钮
- 自动备份 `_tm_snapshot`，可一键恢复
- 已关闭仓位若 close_date > target 会"复活"为 open 仓
- 验证：用 2025-01-01 作为 target 跑出来的现金/股数/realized 与原始 JSON 完全一致

### 4.4 intrinsic value fallback（v5 新增）

```python
def get_mid_smart(df, pos, d, spot) -> tuple[float | None, bool]:
    """报价缺失时对 Long 仓位用 intrinsic value 估算。Short 不估算（不安全）。
    返回 (value, is_estimated)。"""
```

**应用场景**：LEAPS 持仓 DTE 跌出当前数据过滤范围（30-900）或 Δ 超出 0.50-0.95
**误差**：深 ITM (Δ>0.95) 误差 < 2%；中 ITM (Δ 0.5-0.8) 误差 5-10%
**UI 标记**：估算价仓位方向列加 ⋯ 标记 + 表下方 caption 说明

### 4.5 批次成本追踪（v5 新增）

```python
shares[ticker] = {
    "qty": 200,
    "cost_basis": 425.0,                      # 加权平均（兼容字段）
    "lots": [
        {"qty": 100, "price": 430.0, "date": "2024-03-15"},
        {"qty": 100, "price": 420.0, "date": "2025-05-23"},
    ]
}
```

- `_add_lot()`：每次买入/被行权追加批次
- `_sell_lots_fifo()`：Short Call 备兑结算时 FIFO 消耗，返回真实成本（不是平均成本）
- 向后兼容：旧 workspace JSON 无 `lots` 字段时自动合成单一批次

### 4.6 PnL / 现金流公式（v6：含交易成本）

```
fill      = mid ± slippage            (买入 +slip，卖出 -slip；开、平两腿都算)
pnl       = direction × (cur_fill - entry_fill) × 100 × qty - entry_fee
开仓现金流 = -direction × fill × 100 × qty - (order_fee + (per_contract + commission) × qty)
平仓现金流 = +direction × fill × 100 × qty   (返済免费，只有滑点)
到期/行权 = 不收费
```
「💱 交易成本」开关关闭时，滑点和手续费都为 0，公式退回 v5 版本。

---

## 五、市场客观限制（数据抓取必读）

### ⚠️ QC Notebook 不能保存 emoji
QC Cloud 的 `research.ipynb` API 拒绝写入含 `🚀` `⚠️` `✓` 等 Unicode 字符的文件。
**对策**：抓取脚本（`research_*_leaps.py`）保持**纯 ASCII**。Dashboard（本地 Streamlit）不受此限。

### ⚠️ QC Free Object Store 上限 50 MB
`qb.ObjectStore.Save()` 累计超过 50MB 会抛 `StorageLimitExceededException`。
**对策**：
- 脚本里 Save 已包裹 `try/except` 软失败
- 同时写盘到 `Output/` 目录（Cell 5 下载按钮基于此）
- 定期清理 ObjectStore：`for k in qb.ObjectStore.Keys: print(k)` → `qb.ObjectStore.Delete(k)`

### ⚠️ VOO 周度期权 2024 年才上线
2022-2023 VOO CC 只能月度操作，2024 起才有周度期权。

### ⚠️ LEAPS Δ > 0.95 被过滤
QQQ LEAPS 数据 Delta 区间 0.50-0.95。当持有的 LEAPS 因标的暴涨 Δ → 0.99，被排除出数据。
**对策**：intrinsic fallback 已自动覆盖（深 ITM 误差 <2%），不影响策略复盘。

### ⚠️ LEAPS 早期年份选择稀疏
2018-2019 QQQ LEAPS 主要只有 1 月年度系列，每个快照可能只有 1-2 个到期日。从 2020 起到期日多样性大幅上升。
**含义**：完整 Roll 实验建议从 2020 起做复盘。

### ⚠️ Ghost Quotes（QC 数据通病）
全部行 bid=ask（QC 返回理论价，非真实盘口）。远 OTM 无成交合约可能多个行权价 mid 相同。
**对策**：`filter_liquid(LIQUIDITY_MIN=5)` 过滤 + Δ<0.10 橙色软警告 + 主链表灰显。

### ⚠️ 分红盲区
Dashboard 不追踪股息。B&H 对比时手动加回：VOO 4 年累计 ~6% / QQQM 年化 ~0.6% / TQQQ 几乎 0%
详见 `memory/feedback_dividends.md`

---

## 六、`research_qqq_leaps.py` v2 关键配置

```python
TICKER     = "QQQ"
start_time = datetime(2018, 1, 1)
end_time   = datetime(2025, 12, 31)
STORE_KEY  = "QQQ_LEAPS_20180101_20251231_greeks.csv"

option.SetFilter(lambda u: u
    .IncludeWeeklys()
    .Strikes(-30, 5)            # 深 ITM 在下方
    .Expiration(30, 900))       # ★ v2: 下限改 30 覆盖完整生命周期

# 数据过滤
if contract.Right != OptionRight.Call: continue   # 仅 Call
if not (30 <= dte <= 900): continue
if delta_abs < 0.50 or delta_abs > 0.95: continue # 深 ITM 区

SAMPLE_MODE = "weekly"   # weekly / biweekly / daily
```

**预期输出**：约 32 万行 / 37.5 MB / 8 年覆盖 / 311 周快照

---

## 七、核心分析结果（v5 新增章节）

### 7.1 VOO 4 年 CC 复盘对比（已完成）

**100 股纯净基准**（剥离 59 股 buffer）：

| | 税前 | 税后（全清仓） |
|---|---|---|
| Wheel 4 年总收益 | +$20,730 | **+$16,519** |
| B&H 4 年总收益 | +$19,500 | +$15,539 |
| **Wheel 优势** | **+$1,230** | **+$980** |
| 年化超额 | +0.72% | +0.57% |

**关键洞察**：
- Wheel 净期权收益 +$3,730（权利金 - 买回 - 损失）
- 资本利得让步 -$2,500（两次低卖高买）
- **结论：CC 在 VOO 长牛标的上性价比不高，但仍微胜 B&H**

> ⚠️ **方法论修正记录**：早期分析曾错用 `realized_pnl` 和 `premium_collected` 做等式，导致买回成本算成 $3,302（实际 $2,663）。正确方法是**直接累加 closed_positions 的 entry_mid × 100** 作为已开仓权利金，并用**现金流口径**核算（参考 Gemini 方法）。

### 7.2 日本特定口座税收影响（20.315%）

- **加密货币是「雑所得」累进至 55%**（不是 20.315%）— 清仓前看盈亏决定时机
- **B&H 递延税优势**：不卖就不交税，长期持有复利效应大
- **Wheel 强制实现资本利得**：每次行权 + 买回 = 强制缴税
- VOO 4 年 Wheel 累计已缴税 ~$2,383（现金口径已落地）

### 7.3 TQQQ Sell Put 最优 Delta 范围

基于 12 笔实盘 + 反事实模拟：

| 市场状态 | 推荐 Δ | 备注 |
|---------|--------|------|
| 牛市稳态 (200MA 上 + RSI<50) | **0.20-0.25** | 用户实测 = 0.235 (5/5 胜) |
| 反弹追高警惕 | **0.15-0.20** | 用户实测 = 0.183 (7/7 胜) |
| 防御模式 (200MA 下 / VIX>30) | **0.10-0.15** | HANDOVER 已有防御信号 |

**硬性纪律**：
- TQQQ 永远不要 Δ > 0.30（3x 杠杆放大尾部）
- DTE 14 是甜点（θ 衰减效率最佳）
- 不要无脑每周入场，**信号过滤比 Delta 选择更重要**

### 7.4 实盘建议（2026-05-18 评估示例）

TQQQ $69 Put @ 18 DTE，IV Percentile 92%：
- 选 $68 Put @ Δ=0.25 限价 $1.88（不要按 Bid $2.03 卖）
- 必查：QQQ > 200MA / RSI < 50 / 今日阴线 / VIX < 30

---

## 八、TQQQ 2 年模拟结果（v4 完成）

| 指标 | 数值 |
|------|------|
| 区间 | 2022-01-14 → 2023-12-15 |
| 初始资金 | $10,000 |
| 已实现 PnL | **+$4,389.50** |
| 2 年回报率 | **+43.9%** |
| 年化 CAGR | ~19.7% |
| 期权胜率 | 73.7%（28 OTM / 38 关闭） |
| 累计权利金 | $3,041（占总盈利 69%） |
| 被行权次数 | Short Put 5 次 + Short Call 5 次 |

**对比基准**：B&H TQQQ 2022-01→2023-12 ≈ -28%，Wheel 跑赢 70+ 个百分点。

详见 `data/Simulator/workspace_TQQQ_20231215.json`

---

## 九、QuantConnect 平台限制（Free 账户）

| 限制 | 说明 | 已应对 |
|------|------|--------|
| 无 API 访问 | 不能用 REST API 读写 Object Store | 用 Notebook 内置接口 |
| 10KB 日志/天 | main.py 回测日志需精简 | — |
| **Object Store 50MB** | 多次抓取累计会超限 | 软失败 + Output/ 落盘 + 定期清理 |
| **Notebook API 拒绝 emoji** | `🚀` 等 Unicode 字符无法保存 | 抓取脚本纯 ASCII |
| 无 UI 下载入口 | Object Store 无浏览器下载 | Cell 5 HTML base64 按钮 |

---

## 十、下一步建议操作顺序

```
0. ★ v6 新：TQQQ Call vs QQQ Call 对比
   → 先改名 TQQQ 2023 文件、提交 research_tqqq_calls.py
   → 同一买入信号日分别在 TQQQ / QQQ 年度数据上开 Call，比较收益、回撤、手续费占比

1. 等 LEAPS 8 年数据真正用起来：
   → 加载 workspace_QQQLeaps_2026-06-03.json
   → Time Machine 跳到 2020-02 COVID 暴跌前买入 Δ=0.80 LEAPS
   → 观察 2020-04 -33% 标的下 LEAPS MTM 真实损伤
   → 关键学习：2022 -33% QQQ 全年 LEAPS 持有人体验

2. VOO 4 年 CC 模拟跑完 → 已完成 (workspace_VOO_2023_2026.json)
   → 用 7.1 节方法重新做一次纯净 100 股基准对比

3. TQQQ 实盘 1 张 Sell Put 起步（参考 7.4 节）

4. main.py CC 防割肉逻辑 → if CC_Strike < avg_cost: skip
   → 200MA 以下时 Short Put Delta 从 0.25 降至 0.10~0.15

5. 运行 main.py 回测（QuantConnect Algorithm Lab）

6. 保证金冻结 V5
   → Short Put：cash_available -= strike × 100
```

---

## 十一、CCB 工具使用

```bash
python ccb.py "你的问题"
```
自动加载本 HANDOVER.md 作为项目背景，用 `gemini-2.5-pro` 分析后将结果传递给 Claude Code。

---

## v5 工程经验提示

1. **现金流口径 > 权益账面**：策略对比时优先用「现金变化」核算，不要用 `realized_pnl` 反推（含未平仓污染）
2. **QC Notebook 写文件先 ASCII**：emoji 在文档里好看但 QC Save 会拒绝
3. **Object Store 当临时空间**：不要长期堆积，用完即下载
4. **LEAPS 数据 DTE 下限给 30**：不要用 365，否则到期前 1 年全断
5. **Streamlit `st.form` 不重渲染**：动态预览要把 form 拆掉

## v6 工程经验提示

1. **QC 抓取按年分文件**：多年数据一次跑会撞 50MB Object Store 上限，还会被免费账户的会话超时终止 → 设置 `YEAR`，每年 Run All → 下载 → 重启 Notebook
2. **成本模型用实盘成交确认书校准**：MooMoo 平仓免费，往返成本不是开仓费 × 2
3. **数据过滤范围要覆盖「赢家」**：买 Call 研究里 Δ 上限给到 0.95、DTE 下限给到 1，否则进 ITM 的赚钱仓位和临近到期的平仓都会从数据里消失
4. **Streamlit selectbox 跨步进保持选中**：用固定 `key` + session_state 镜像，否则日期一变选项列表重建就会跳回第一项

---

## 十二、QQQ 历史期权数据下载流程（2026-11-09 实测，Free 账户）

目标：补齐 QQQ 2012–2020 与 2026 年的短期期权链（与 2021–2025 文件同口径），供 `auto_wheel.py` 做 2012–2026 的 3 年随机回测。全程由 Claude 在 Claude 桌面版内置浏览器里操作用户已登录的 QC 账户完成。

### 12.1 结论先看

| 项目 | 结果 |
|------|------|
| 可用年份 | **2012–2026**（2007–2011 年 `OptionChain` 全部为空，QC 的 QQQ 期权数据从 2012 年开始） |
| 抓取速度 | 很快：每年 12–26 秒（2026 年 28 秒），整批 2012–2020 约 2.5 分钟 |
| 口径 | 每个交易日 15:45 `qb.OptionChain`，DTE 1–60，\|Δ\| 0.05–0.70，列与旧文件完全一致 |
| 校验 | 重抓 2021-01 与本地旧文件对比：12,877 行**逐行一致**（mid/Δ/IV/volume/OI 差值全为 0） |
| 早年注意 | 2012 年只有 173/248 天有 7–21 天到期合约（多为月度期权）；2013 年起每天都有 |

### 12.2 走不通的路（别再试）

1. **回测（Backtest）里写 Object Store**：Free 账户报错 `The current user does not have permission to write to the organization Object Store`。
2. **项目代码里 `import gzip` 压缩**：QC 保存文件时直接拒绝（ToS 禁止在项目源码里压缩/编码/嵌入二进制）。不要尝试绕过。
3. **直接读 Research 输出文件**：Notebook 内核跑在独立的 Research 服务器上（cwd = `/QuantConnect/research-cloud/airlock`），编辑器（code-server）的文件系统看不到它的 `Output/`。
4. **VS Code 下载对话框里的 “Show Local”**：在内置浏览器中不会落地成文件。

### 12.3 实际可行的流程

1. **登录**：内置浏览器若显示 “session expired” 且点 Log In 无反应，是残留了过期会话（首页显示 Sign Out，/login 被重定向回 /lab）。先打开 `https://www.quantconnect.com/loggedout` 再登录。
2. **建项目**：在已登录页面里用会话调用 QC API（`fetch('/api/v2/projects/create', …)`，`files/update` 写 `research.ipynb`），新建了项目 `QQQ_Options_Dump`。
3. **运行 Notebook**：打开项目编辑器（VS Code / code-server），打开 `research.ipynb` → Select Kernel → *QuantConnect Research Server Collection* → `Foundation-Py-Default` → Run All。
   - 代码与 `research_qqq_options_history.py` 相同：`AddEquity(Raw)` + `AddOption`，逐日 `qb.SetDateTime(当天 15:45)` + `qb.OptionChain`，按年写 `Output/QQQ_options_<Y>0101_<Y>1231_greeks.csv`。
   - **坑 1**：`SetDateTime` 把 QuantBook 时钟拨回过去后，再请求今年的数据会返回空（History 截止到当前 qb 时间）。抓当年（2026）前先 `qb.SetDateTime(datetime.now())`。
   - **坑 2**：第一次用了 \|Δ\| ≤ 0.60，比旧文件少了 0.60–0.70 的实值合约；改为 0.70 后与旧文件一致。
   - **坑 3**：VS Code 的 Jupyter 变量面板检查大对象时会让内核假死，必要时 Restart（已写入磁盘的 CSV 不受影响）。
4. **把文件弄到本地**（关键）：
   1. 在 Notebook 里把所有 CSV 串成一个 bundle（每个文件前加一行 `=====FILE <文件名>`），按 15 MB 切成 8 段，并打印总长度和 MD5。
   2. 每段用 `display(HTML('<a download=... href="data:application/octet-stream;base64,...">'))` 生成下载链接；点击后 VS Code 弹出“保存到哪里”，把路径改成 `/home/lean-user/workspace/qqq_bundle.partNN`（**放在 project 文件夹外**，避免大文件被同步进 QC 项目）。
   3. 在编辑器页面用 `fetch('<code-server>/vscode-remote-resource?path=…')` 读回 8 段，拼成一个 Blob，用 `<a download>` 触发浏览器下载 → 落到 `C:\Users\kanun\Downloads\` 下的 `.tmp` 文件。
   4. 本地校验 MD5（`8aac5e50ae0e4bcfbe51aac01e9444f0`，118,918,305 bytes）一致后，按 `=====FILE` 拆回各 CSV，放入 `data/`（现已移到 `data/QQQ/`）。
5. **QC 账户里留下的东西**：项目 `QQQ_Options_Dump`（含 `research.ipynb`）、编辑器工作区里约 119 MB 的 `qqq_bundle.part00–07`，以及 Research 服务器上的 `Output/`、`Output2/`。不再需要可直接在 QC 删除该项目。

### 12.4 以后要更新数据（例如补 2026 年 11–12 月）

最省事：打开 `QQQ_Options_Dump` 项目的 Notebook，新建一个 cell，`qb.SetDateTime(datetime.now())` 后调用 `fetch(datetime(2026,1,1), datetime.now())` 写 CSV，再按 12.3 第 4 步打包下载。单个文件 < 15 MB 时可直接一个链接下载，不必切段。
