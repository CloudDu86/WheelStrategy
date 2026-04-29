#!/usr/bin/env python3
"""
ccb.py — Gemini × Claude Code Collaborative Bridge
Usage: python ccb.py "your task description"
"""

import sys
import os
import time
import threading
import subprocess
import shutil

# Windows ターミナルで絵文字が化けないよう UTF-8 強制
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── colour / style helpers ────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
RED    = "\033[91m"
PURPLE = "\033[95m"
DIM    = "\033[2m"

def cprint(color, msg):
    print(f"{color}{msg}{RESET}")

def banner():
    print()
    cprint(CYAN,   "╔══════════════════════════════════════════════════════════╗")
    cprint(CYAN,   "║         CCB — Gemini × Claude Code Bridge  v1.0         ║")
    cprint(CYAN,   "╚══════════════════════════════════════════════════════════╝")
    print()

# ── spinner animation ─────────────────────────────────────────────────────────
class Spinner:
    FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

    def __init__(self, label: str, color: str = CYAN):
        self.label   = label
        self.color   = color
        self._stop   = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            sys.stdout.write(f"\r{self.color}{BOLD}{frame} {self.label}{RESET}   ")
            sys.stdout.flush()
            time.sleep(0.08)
            i += 1

    def start(self):
        self._thread.start()
        return self

    def stop(self, success: bool = True):
        self._stop.set()
        self._thread.join()
        icon = f"{GREEN}✔{RESET}" if success else f"{RED}✘{RESET}"
        sys.stdout.write(f"\r{icon} {BOLD}{self.label}{RESET}              \n")
        sys.stdout.flush()

# ── progress bar ──────────────────────────────────────────────────────────────
def progress_bar(label: str, color: str, duration: float = 3.0, width: int = 40):
    """Fake determinate bar used as a visual beat while we wait."""
    steps = 50
    for i in range(steps + 1):
        pct   = i / steps
        filled = int(width * pct)
        bar   = "█" * filled + "░" * (width - filled)
        sys.stdout.write(f"\r{color}{BOLD}{label}{RESET} [{color}{bar}{RESET}] {int(pct*100):3d}%")
        sys.stdout.flush()
        time.sleep(duration / steps)
    print()

# ── pexpect availability ──────────────────────────────────────────────────────
try:
    import pexpect
    HAS_PEXPECT = True
except ImportError:
    HAS_PEXPECT = False

# ── command runners ───────────────────────────────────────────────────────────
def run_with_pexpect(cmd: list[str], auto_confirm: bool = True) -> int:
    """Run command via pexpect, auto-answering y/n prompts."""
    child = pexpect.spawn(" ".join(cmd), encoding="utf-8", timeout=600)
    patterns = [
        pexpect.EOF,
        r"[Pp]roceed\? \([yY]/[nN]\)",
        r"[Cc]ontinue\? \([yY]/[nN]\)",
        r"\[y/N\]",
        r"\[Y/n\]",
    ]
    while True:
        idx = child.expect(patterns)
        if idx == 0:
            break
        # all prompt patterns → send 'y'
        child.sendline("y")
    child.wait()
    return child.exitstatus or 0


def resolve_cmd(name: str) -> str:
    """
    Find the real executable path for a CLI tool.
    On Windows, npm-installed tools may only exist as .cmd wrappers.
    """
    found = shutil.which(name)
    if found:
        return found
    # Windows fallback: try npm global bin dir
    npm_bin = os.path.join(os.environ.get("APPDATA", ""), "npm", name + ".cmd")
    if os.path.isfile(npm_bin):
        return npm_bin
    return name  # let it fail naturally


def run_subprocess(cmd: list[str]) -> int:
    """Fallback: run via subprocess, piping stdin from /dev/null."""
    result = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        shell=(sys.platform == "win32"),  # .cmd files need shell=True on Windows
    )
    return result.returncode


def run_command(cmd: list[str]) -> int:
    if HAS_PEXPECT:
        return run_with_pexpect(cmd)
    return run_subprocess(cmd)

ANALYSIS_FILE = "gemini_analysis.md"
DEFAULT_MODEL  = "gemini-2.5-pro"  # gemini-3.1-pro-preview は API 未提供、2.5-pro にフォールバックされる

# ── project context loader ─────────────────────────────────────────────────────
def load_project_context() -> str:
    """Read HANDOVER.md (or README.md) from cwd as project background."""
    for fname in ("HANDOVER.md", "README.md"):
        if os.path.isfile(fname) and os.path.getsize(fname) > 0:
            with open(fname, encoding="utf-8", errors="replace") as f:
                return f.read()
    return ""


# ── phase 1: Gemini analyzes with full project context ────────────────────────
def phase_gemini(task: str, model: str = DEFAULT_MODEL) -> str | None:
    """
    Run `gemini -m <model> -p "<prompt>"` (headless mode) with project context
    prepended. Captures stdout, saves to ANALYSIS_FILE.
    Returns the analysis text on success, None on failure.
    """
    print()
    cprint(YELLOW, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    cprint(YELLOW, f"  🤖  PHASE 1 — Gemini ({model}) 分析中…")
    cprint(YELLOW, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    # ── 构建富含项目背景的 Prompt ─────────────────────────────────────────────
    project_ctx = load_project_context()
    if project_ctx:
        cprint(DIM, f"  ✔  已加载项目背景（HANDOVER.md，{len(project_ctx)} 字符）")
        full_prompt = (
            f"## 项目背景\n\n"
            f"{project_ctx}\n\n"
            f"---\n\n"
            f"## 当前任务\n\n"
            f"{task}\n\n"
            f"---\n\n"
            f"## 分析要求\n\n"
            f"你已了解上方的完整项目背景（车轮期权策略回测系统）。"
            f"请结合项目目标，对当前任务给出**有实际指导意义的深入分析**：\n"
            f"- 不要只描述数据结构，要指出对策略回测的影响\n"
            f"- 给出具体、可操作的优化建议\n"
            f"- 指出任何数据质量问题或潜在陷阱\n"
            f"- 结合 2022 年 TQQQ 的市场背景（熊市、高波动）给出判断"
        )
    else:
        cprint(DIM, "  ⚠  未找到 HANDOVER.md，使用裸任务描述")
        full_prompt = task

    gemini_exe = resolve_cmd("gemini")

    spinner = Spinner("Gemini 正在深度分析，请稍候…", YELLOW).start()

    # 将完整 prompt 写入临时文件，再用 Gemini 的 --input 参数读取，
    # 避免 Windows CMD 参数长度限制和 stdin 管道超时问题。
    import tempfile
    prompt_file = None

    if project_ctx:
        full_prompt = (
            f"## 项目背景\n\n{project_ctx}\n\n"
            f"---\n\n"
            f"## 当前任务\n\n{task}\n\n"
            f"---\n\n"
            f"## 分析要求\n\n"
            f"你已了解上方的完整项目背景（车轮期权策略回测系统）。"
            f"请结合项目目标，给出**有实际指导意义的深入分析**：\n"
            f"- 不要只描述数据结构，要指出对策略回测的实际影响\n"
            f"- 给出具体可操作的优化建议\n"
            f"- 指出数据质量问题或潜在陷阱\n"
            f"- 结合 2022 年 TQQQ 熊市/高波动背景给出判断"
        )
    else:
        full_prompt = task

    try:
        # 写入临时文件（UTF-8，避免编码问题）
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", encoding="utf-8",
            delete=False, prefix="ccb_prompt_"
        ) as tf:
            tf.write(full_prompt)
            prompt_file = tf.name

        # 用 type 命令管道到 gemini（Windows shell 方式），避免长参数截断
        # gemini -m <model> -p "" 配合 stdin 读取文件内容
        pipe_cmd = f'type "{prompt_file}" | {gemini_exe} -m {model} -p ""'

        result = subprocess.run(
            pipe_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            shell=True,   # 必须用 shell=True 才能执行 type | pipe
        )
        output = (result.stdout or "").strip()

        # 部分版本将回答写入 stderr
        if not output and result.stderr:
            stderr_clean = result.stderr.strip()
            if len(stderr_clean) > 50:   # 真正的内容，而非错误消息
                output = stderr_clean

        spinner.stop(success=bool(output))

        if not output:
            cprint(RED, f"  [ERROR] Gemini 无输出（退出码 {result.returncode}）")
            if result.stderr:
                cprint(DIM, f"  stderr: {result.stderr[:400]}")
            return None

    except subprocess.TimeoutExpired:
        spinner.stop(success=False)
        cprint(RED, "  [ERROR] Gemini 超时（>600s）")
        return None
    except FileNotFoundError:
        spinner.stop(success=False)
        cprint(RED, "  [ERROR] 找不到 gemini 命令，请确认已安装并在 PATH 中")
        return None
    finally:
        if prompt_file and os.path.isfile(prompt_file):
            os.unlink(prompt_file)

    # ── 保存到文件供参考 ──────────────────────────────────────────────────────
    with open(ANALYSIS_FILE, "w", encoding="utf-8") as f:
        f.write(f"# Gemini 分析报告\n\n")
        f.write(f"**原始问题：** {task}\n\n")
        f.write("---\n\n")
        f.write(output)
        f.write("\n")

    cprint(GREEN, f"  ✔  Gemini 分析完成，已保存至 {ANALYSIS_FILE}")
    print()

    # ── 在终端打印 Gemini 回答（让用户知道内容）────────────────────────────────
    cprint(YELLOW, "  ┌─ Gemini 的分析 ───────────────────────────────────────")
    for line in output.splitlines():
        print(f"  │ {DIM}{line}{RESET}")
    cprint(YELLOW, "  └────────────────────────────────────────────────────────")
    print()

    return output


# ── phase 2: format Gemini's analysis for Claude Code to consume ──────────────
def phase_claude(task: str, gemini_output: str) -> bool:
    """
    Print a structured handoff message.
    Since ccb.py is typically run inside a Claude Code session, the output
    of this phase appears directly in the conversation — Claude reads it
    and acts on it without needing a separate subprocess.
    """
    print()
    cprint(PURPLE, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    cprint(PURPLE, "  🔨  PHASE 2 — 移交给 Claude Code")
    cprint(PURPLE, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    print(f"{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  [CCB → Claude Code] 以下是 Gemini 的分析，请据此行动{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    print()
    print(f"{CYAN}  原始问题：{RESET}{task}")
    print()
    print(f"{YELLOW}  Gemini 分析（完整版见 {ANALYSIS_FILE}）：{RESET}")
    print()
    for line in gemini_output.splitlines():
        print(f"  {line}")
    print()
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  请 Claude Code：{RESET}")
    print(f"  1. 评估上述分析的准确性，指出任何错误")
    print(f"  2. 在当前项目中实施必要的改动")
    print(f"  3. 如与自身判断不符，优先按自身判断行事并说明原因")
    print(f"{BOLD}{'='*60}{RESET}")
    print()
    return True

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    banner()

    if len(sys.argv) < 2:
        cprint(RED, "用法: python ccb.py \"<任务描述>\"")
        sys.exit(1)

    task = " ".join(sys.argv[1:])

    cprint(BOLD, f"  任务: {task}")
    print()
    cprint(DIM, f"  pexpect 可用: {'是' if HAS_PEXPECT else '否 (使用 subprocess 回退)'}")
    print()

    # check required CLIs exist
    # gemini は npm 経由で .cmd として入っていることが多いので候補を複数試す
    missing = [cli for cli in ("gemini", "claude") if not shutil.which(cli)]
    if missing:
        cprint(RED, f"  [ERROR] 以下命令未找到，请确保已安装并在 PATH 中: {', '.join(missing)}")
        sys.exit(1)

    # ── phase 1: Gemini 分析 ──
    gemini_output = phase_gemini(task)
    if not gemini_output:
        cprint(RED, "\n  流程中止：Gemini 阶段失败。")
        sys.exit(1)

    progress_bar("桥接 Gemini → Claude…", CYAN, duration=1.0)

    # ── phase 2: Claude Code 接收并实施 ──
    ok = phase_claude(task, gemini_output)
    if not ok:
        cprint(RED, "\n  流程中止：Claude 阶段失败。")
        sys.exit(1)

    # ── done ──
    print()
    cprint(GREEN,  "╔══════════════════════════════════════════════════════════╗")
    cprint(GREEN,  "║              🎉  全部完成！任务交付成功  🎉              ║")
    cprint(GREEN,  "╚══════════════════════════════════════════════════════════╝")
    print()


if __name__ == "__main__":
    main()
