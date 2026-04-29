# region imports
from AlgorithmImports import *
# endregion

class ReadObjectStore(QCAlgorithm):
    def Initialize(self):
        self.SetStartDate(2026, 4, 15)
        self.SetEndDate(2026, 4, 16)
        self.SetCash(100000)

        keys = [
            "options_chain_QQQ_TQQQ_combined.csv",
            "options_chain_QQQ.csv",
            "options_chain_TQQQ.csv",
        ]

        for key in keys:
            if self.ObjectStore.ContainsKey(key):
                content = self.ObjectStore.Read(key)
                lines   = content.strip().split("\n")
                self.Log(f"\n{'='*60}")
                self.Log(f"FILE: {key}  ({len(lines)-1} data rows)")
                self.Log(f"{'='*60}")
                # Print header + first 10 rows
                for line in lines[:11]:
                    self.Log(line)
                if len(lines) > 11:
                    self.Log(f"... ({len(lines)-11} more rows)")
                # Print full CSV for copy-paste (comment out if too large)
                self.Log("\n── FULL CSV ──")
                self.Log(content)
            else:
                self.Log(f"[NOT FOUND] {key}  — 请先运行数据提取脚本")
