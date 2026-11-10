"""真实任务探测：建会话、发一条 prompt、打印全量事件与聚合结果。

注意，这个脚本会真发 prompt，消耗模型额度。默认只打印计划，必须显式传 --run 才执行。
它会：建 1 个会话，发 1 条 prompt，订阅事件直到 session.idle 或超时，最后删掉探测会话。

用法：
    .venv/bin/python tests/probe_opencode.py --dry-run
    .venv/bin/python tests/probe_opencode.py --run
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import logger
from src.opencode_client import (
    OpenCodeClient,
    delta_text,
    ended_text,
    event_type,
    is_idle,
    text_ref,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_REQUESTS = 3  # 建会话 1 次、发 prompt 1 次、删会话 1 次


def parse_args():
    from dotenv import load_dotenv

    from src import opencode_service

    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="真实任务事件探测，会消耗模型额度")
    parser.add_argument("--base-url", default=os.getenv("PROBE_BASE_URL")
                        or os.getenv("OPENCODE_BASE_URL") or "auto")
    parser.add_argument("--password", default=os.getenv("PROBE_PASSWORD")
                        or os.getenv("OPENCODE_PASSWORD") or "")
    parser.add_argument("--dir", default=str(PROJECT_ROOT))
    parser.add_argument("--prompt", default="回复两个字：收到")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--run", action="store_true", help="真的发 prompt，否则只打印计划")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不发任何请求")
    args = parser.parse_args()
    if args.dry_run and args.run:
        parser.error("--dry-run 与 --run 互斥")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if opencode_service.is_auto(args.base_url):
        endpoint = opencode_service.resolve(args.base_url, os.getenv("OPENCODE_USERNAME") or "", args.password)
        args.base_url = endpoint["base_url"]
        args.password = endpoint["password"]
    if not args.password and not args.dry_run:
        parser.error("缺少密码：用 --password 或设置环境变量 OPENCODE_PASSWORD")
    return args


def headers(password):
    import base64

    if not password:
        return {}
    raw = f"opencode:{password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def aggregate(events):
    slots = {}
    for ev in events:
        t = event_type(ev)
        if t == "session.text.started":
            slots.setdefault(text_ref(ev), "")
        elif t == "session.text.delta":
            slots[text_ref(ev)] = slots.get(text_ref(ev), "") + delta_text(ev)
        elif t == "session.text.ended":
            full = ended_text(ev)
            if full is not None:
                slots[text_ref(ev)] = full
    return "".join(slots.values())


async def run(args):
    client = OpenCodeClient(args.base_url, headers(args.password), timeout=args.timeout)
    queue = asyncio.Queue()
    stop = asyncio.Event()
    reader = asyncio.create_task(client.stream_events(queue, stop))

    session = await client.create_session(args.dir, title="probe 探测会话")
    sid = session["id"]
    print("新建会话:", sid)

    events = []
    samples = {}
    prompt_task = asyncio.create_task(client.send_prompt(sid, args.prompt))
    deadline = asyncio.get_event_loop().time() + args.timeout

    try:
        while asyncio.get_event_loop().time() < deadline:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                if prompt_task.done():
                    exc = prompt_task.exception()
                    if exc:
                        logger.error(f"prompt 失败: {exc}")
                        break
                continue
            events.append(ev)
            t = event_type(ev)
            samples.setdefault(t, json.dumps(ev, ensure_ascii=False))
            if t not in ("server.connected", "session.updated"):
                print(f"[{t}] {json.dumps(ev, ensure_ascii=False)[:220]}")
            if is_idle(ev) or t == "session.execution.succeeded":
                break
    finally:
        stop.set()
        reader.cancel()
        try:
            await reader
        except BaseException:
            pass

    samples_path = Path(__file__).resolve().parent.parent / "logs" / "probe_samples.json"
    samples_path.parent.mkdir(parents=True, exist_ok=True)
    samples_path.write_text(
        json.dumps(samples, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("完整事件样本已写入", samples_path)

    print("-" * 60)
    counts = {}
    for ev in events:
        t = event_type(ev)
        counts[t] = counts.get(t, 0) + 1
    print("事件类型统计:")
    for k in sorted(counts):
        print(f"   {k}: {counts[k]}")
    text = aggregate(events)
    print(f"聚合后文本长度: {len(text)}")
    print("聚合后文本前 300 字:", text[:300])

    try:
        await client.delete_session(sid)
        print("探测会话已删除")
    except Exception as exc:
        print("删除探测会话失败:", exc)

    if "session.execution.succeeded" not in counts and "session.idle" not in counts:
        print("未观察到完成信号（session.execution.succeeded 或 session.idle）")
        return 1
    return 0


def main():
    args = parse_args()
    if args.dry_run or not args.run:
        print("端点:", f"{args.base_url}/api")
        print("用户名: opencode")
        print("密码:", "已配置" if args.password else "未配置")
        print("探测目录:", args.dir)
        print("prompt 内容:", args.prompt)
        print(f"请求数上界: {MAX_REQUESTS} 次（建会话、发 prompt、删会话各 1 次）")
        print("事件订阅: GET /api/event 1 条长连接")
        print("未传 --run，不发起任何请求。加 --run 真跑，会消耗模型额度。")
        return 0
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
