"""
将交易流水 CSV 转换为 Dashboard 可加载的 JSON 工作区文件。
用法: python csv_to_workspace.py <csv_path> [--capital 10000] [--ticker TQQQ]
"""
import json
import re
import sys
import argparse
from datetime import date
import pandas as pd


def reconstruct(df: pd.DataFrame, initial_capital: float, ticker: str) -> dict:
    account = {
        "initial_capital": initial_capital,
        "cash": initial_capital,
        "collateral": 0.0,
        "shares": {},
        "realized_pnl": 0.0,
        "history": [],
        "equity_cost": {},
        "premium_collected": {},
    }

    for _, row in df.iterrows():
        trade_date = row["日期"]
        action     = str(row["动作"]).strip()
        symbol     = str(row["合约代码"]).strip()
        qty        = int(row["张数"])
        price      = float(row["价格"])
        cash_d     = float(row["现金变化"])
        real       = float(row["已实现PnL"])
        note       = str(row["备注"])

        # 1. 追加流水
        account["history"].append({
            "date":       trade_date,
            "action":     action,
            "symbol":     symbol,
            "qty":        qty,
            "price":      price,
            "cash_delta": cash_d,
            "realized":   real,
            "note":       note,
        })

        # 2. 现金 & 已实现 PnL
        account["cash"]         += cash_d
        account["realized_pnl"] += real

        # 3. 分类处理

        if action in ("Short Put 被行权 - 买入股票", "直接买入股票"):
            # symbol 是 ticker (e.g. "TQQQ")
            cost = price * qty
            ec = account["equity_cost"]
            ec[symbol] = ec.get(symbol, 0.0) + cost
            sh = account["shares"]
            if symbol not in sh:
                sh[symbol] = {"qty": 0, "cost_basis": price}
            old_qty   = sh[symbol]["qty"]
            old_basis = sh[symbol]["cost_basis"]
            new_qty   = old_qty + qty
            sh[symbol]["cost_basis"] = (old_qty * old_basis + qty * price) / new_qty
            sh[symbol]["qty"]        = new_qty

        elif action == "Short Call 被行权 - 卖出股票 (备兑)":
            # symbol 是 ticker
            sh = account["shares"]
            if symbol in sh:
                sh[symbol]["qty"] -= qty
                if sh[symbol]["qty"] <= 0:
                    sh[symbol]["qty"] = 0
                    # 轮次结束 → 重置本轮成本追踪
                    account["equity_cost"][symbol]      = 0.0
                    account["premium_collected"][symbol] = 0.0

        elif action.startswith("开仓 Short"):
            # 从合约代码提取 ticker（取第一个空格前的部分）
            t = symbol.split()[0] if " " in symbol else ticker
            pc = account["premium_collected"]
            pc[t] = pc.get(t, 0.0) + abs(cash_d)

        # 其余动作（到期废弃、手动平仓、开仓 Long）现金和已实现已在上面处理

    return account


def date_serial(obj):
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Not serializable: {type(obj)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--capital", type=float, default=10000.0)
    parser.add_argument("--ticker",  default="TQQQ")
    args = parser.parse_args()

    df = pd.read_csv(args.csv_path)
    df.columns = df.columns.str.strip()
    df["日期"] = pd.to_datetime(df["日期"]).dt.date
    df = df.sort_values("日期").reset_index(drop=True)

    if df.empty:
        print("CSV 为空，退出。")
        sys.exit(1)

    account = reconstruct(df, args.capital, args.ticker)

    # 序列化 history 中的 date
    acc_copy = dict(account)
    acc_copy["history"] = [
        {**h, "date": h["date"].isoformat()} for h in account["history"]
    ]

    last_date = df["日期"].max()
    payload = {
        "version":          1,
        "active_ticker":    args.ticker,
        "current_date_idx": None,        # Dashboard 启动时自动定位
        "last_csv_date":    last_date.isoformat(),
        "initial_capital":  args.capital,
        "account":          acc_copy,
        "positions":        [],           # 未平仓期权需手动重建
        "closed_positions": [],
    }

    # 输出到同目录下
    import os
    out_dir  = os.path.dirname(os.path.abspath(args.csv_path))
    csv_name = os.path.splitext(os.path.basename(args.csv_path))[0]
    out_path = os.path.join(out_dir, f"{csv_name}_workspace.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=date_serial)

    # 打印摘要
    sh = account["shares"]
    print(f"\n✅ 已生成: {out_path}")
    print(f"   初始资金 : ${args.capital:,.2f}")
    print(f"   剩余现金 : ${account['cash']:,.2f}")
    print(f"   已实现PnL: ${account['realized_pnl']:+,.2f}")
    print(f"   流水条数 : {len(account['history'])}")
    print(f"   末尾日期 : {last_date}")
    print(f"   股票持仓 :")
    for t, s in sh.items():
        if s["qty"] > 0:
            ec = account["equity_cost"].get(t, 0)
            pc = account["premium_collected"].get(t, 0)
            dyn = (ec - pc) / s["qty"] if s["qty"] > 0 else None
            print(f"     {t}: {s['qty']} 股 | 账面成本 ${s['cost_basis']:.2f} | 动态成本 ${dyn:.2f}")


if __name__ == "__main__":
    main()
