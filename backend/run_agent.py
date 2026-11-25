"""TracePilot Agent 命令行入口（第 14 步）。

用法：
    python backend/run_agent.py "任务描述"
    python backend/run_agent.py --task "任务描述" --no-tools

行为：
- 自动加载项目根目录的 .env（若不存在则静默跳过）；
- 调用 LangGraph Agent（DeepSeek API，支持工具调用）；
- 依次输出：任务理解、执行计划、工具调用明细（名称 / 参数 / 结果）、最终回答；
- 捕获并友好提示 API 错误（缺 Key、网络失败、超时等）；
- 绝不输出 API Key 等敏感信息。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    # 与后端一致：backend/.env 优先，项目根 .env 兼容旧配置
    for _env_path in (PROJECT_ROOT / "backend" / ".env", PROJECT_ROOT / ".env"):
        if _env_path.is_file():
            load_dotenv(_env_path)
except ImportError:
    pass

from backend.app.agents.basic_agent import AgentError, AgentResult, run_agent  # noqa: E402


def _print_result(result: AgentResult) -> None:
    """按区块打印 Agent 执行结果（不含任何密钥信息）。"""
    print("=" * 60)
    print("【任务理解】")
    print(result.understanding or "（无）")
    print()
    print("【执行计划】")
    for i, step in enumerate(result.plan, 1):
        print(f"  {i}. {step}")
    print()
    print("【工具调用】")
    if result.tool_executions:
        for item in result.tool_executions:
            print(f"  - {item.get('name', '?')}")
            print(f"    参数: {json.dumps(item.get('arguments'), ensure_ascii=False)}")
            print(f"    结果: {json.dumps(item.get('result'), ensure_ascii=False, default=str)}")
    else:
        print("  （本次未调用工具）")
    print()
    print("【最终回答】")
    print(result.result)
    print("=" * 60)
    print(
        f"[model={result.model} | 耗时 {result.elapsed_ms}ms | "
        f"工具调用 {result.tool_call_count} 次]"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_agent",
        description="TracePilot Agent 命令行入口（DeepSeek API）",
    )
    parser.add_argument("task", nargs="?", help="任务描述（含空格请加引号）")
    parser.add_argument("--no-tools", action="store_true", help="禁用工具调用")
    parser.add_argument(
        "--model", default=None, help="覆盖模型名（默认读环境变量 DEEPSEEK_MODEL / 内置默认）"
    )
    parser.add_argument(
        "--save-trace", action="store_true",
        help="把本次执行的 Trace 保存到 data/traces/（文件名 = trace_id）",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    task = args.task
    if not task or not task.strip():
        print('未提供任务。用法: python backend/run_agent.py "任务描述"', file=sys.stderr)
        return 2

    result: AgentResult = await run_agent(
        task.strip(),
        tools=[] if args.no_tools else None,
        model=args.model,
        save_trace=args.save_trace,
    )
    _print_result(result)
    if result.trace:
        print(f"[Trace] trace_id={result.trace.trace_id} | 步骤数={len(result.trace.steps)}")
    if result.trace_path:
        print(f"[Trace 已保存] {result.trace_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except AgentError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        print(
            "提示：请检查 .env 中的 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL 配置与网络连接。",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"[错误] 保存 Trace 失败: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[中断] 已取消执行。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
