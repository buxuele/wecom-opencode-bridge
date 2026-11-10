"""只读事件探针：订阅 opencode serve 的 SSE 事件流，记录事件类型与样本。

不发 prompt、不建会话、不改任何状态，只订阅 GET /api/event。
用途：bridge 改动后核对真实事件结构，重点是 session.text.started/delta/ended
的字段路径、session.execution.succeeded 完成信号，以及 permission.asked 的形状。

用法：
    .venv/bin/python tests/probe_events.py --dry-run
    .venv/bin/python tests/probe_events.py --seconds 60
"""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "event_samples.txt"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def parse_args():
    sys.path.insert(0, str(PROJECT_ROOT))
    from dotenv import load_dotenv

    from src import opencode_service

    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="只读订阅 opencode 共享服务事件流")
    parser.add_argument("--base-url", default=os.getenv("OPENCODE_BASE_URL") or "auto")
    parser.add_argument("--username", default=os.getenv("OPENCODE_USERNAME") or "opencode")
    parser.add_argument("--password", default=os.getenv("OPENCODE_PASSWORD") or "")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将用的端点与凭据是否配置，不发任何连接")
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("--seconds 必须大于 0")
    if opencode_service.is_auto(args.base_url):
        endpoint = opencode_service.resolve(args.base_url, args.username, args.password)
        args.base_url = endpoint["base_url"]
        args.password = endpoint["password"]
    if not args.password and not args.dry_run:
        parser.error("缺少密码：用 --password 或设置环境变量 OPENCODE_PASSWORD")
    return args


async def run(args):
    import base64

    import httpx

    headers = {
        "Accept": "text/event-stream",
        "Authorization": "Basic "
        + base64.b64encode(f"{args.username}:{args.password}".encode()).decode(),
    }
    counter = Counter()
    samples = {}
    deadline = asyncio.get_event_loop().time() + args.seconds
    async with httpx.AsyncClient(
        base_url=args.base_url, headers=headers, timeout=None, trust_env=False
    ) as client:
        async with client.stream("GET", "/api/event") as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")[:300]
                raise RuntimeError(f"订阅失败 HTTP {resp.status_code}: {body}")
            print(f"订阅已建立，持续 {args.seconds} 秒，按 Ctrl+C 可提前结束")
            async for line in resp.aiter_lines():
                if asyncio.get_event_loop().time() >= deadline:
                    break
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type") or "unknown"
                counter[etype] += 1
                if etype not in samples:
                    samples[etype] = raw

    lines = ["# opencode serve 事件样本", f"# base_url: {args.base_url}", ""]
    for etype, count in counter.most_common():
        lines.append(f"## {etype} x{count}")
        lines.append(samples[etype])
        lines.append("")
    Path(args.output).write_text("\n".join(lines), encoding="utf-8")
    print(f"事件类型统计: {dict(counter)}")
    print(f"样本已写入 {args.output}")


def main():
    args = parse_args()
    if args.dry_run:
        print(f"端点: {args.base_url}/api/event")
        print(f"用户名: {args.username}")
        print(f"密码: {'已配置' if args.password else '未配置'}")
        print(f"持续秒数: {args.seconds}")
        print(f"输出文件: {args.output}")
        print("dry-run 模式，不建立任何连接")
        return 0
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("已手动停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
