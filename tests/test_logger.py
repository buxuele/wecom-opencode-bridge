import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import logger

FAILURES = []


def check(name, actual, predicate, detail=""):
    ok = predicate(actual)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def main():
    orig_dir = logger.LOG_DIR
    orig_handles = dict(logger._handles)
    with tempfile.TemporaryDirectory() as raw:
        logger.LOG_DIR = Path(raw) / "logs"
        logger._handles = {}
        try:
            logger.info("测试信息")
            logger.error("测试错误")

            run_log = logger.LOG_DIR / "bridge.log"
            check("运行日志文件已建", run_log.exists(), lambda v: v is True)
            lines = run_log.read_text(encoding="utf-8").splitlines()
            check("运行日志双写级别", lines, lambda ls: "[INFO]" in ls[0] and "[ERROR]" in ls[1], str(lines))
            check("运行日志带日期", lines[0], lambda l: l[:4].isdigit(), lines[0])

            logger.chat("收", "single:u1", "text", "你好")
            logger.chat("发", "single:u1", "stream", "好的")
            chat_log = logger.LOG_DIR / "chat.jsonl"
            check("聊天记录文件已建", chat_log.exists(), lambda v: v is True)
            records = [json.loads(x) for x in chat_log.read_text(encoding="utf-8").splitlines()]
            check("聊天记录两条", records, lambda r: len(r) == 2, str(len(records)))
            check(
                "聊天记录字段齐全",
                records[0],
                lambda r: set(r) >= {"ts", "dir", "key", "kind", "content"},
                str(records[0]),
            )
            check("聊天记录方向正确", records, lambda r: r[0]["dir"] == "收" and r[1]["dir"] == "发")
            check("聊天记录内容正确", records, lambda r: r[0]["content"] == "你好" and r[1]["content"] == "好的")

            long_text = "长" * 2500
            logger.chat("发", "single:u1", "stream", long_text)
            records = [json.loads(x) for x in chat_log.read_text(encoding="utf-8").splitlines()]
            check("超长内容被截断", records[-1]["truncated"], lambda v: v is True)
            check("截断有长度上限", records[-1]["content"], lambda c: len(c) <= logger.CHAT_CONTENT_LIMIT + 1)

            orig_max = logger.MAX_BYTES
            logger.MAX_BYTES = 400
            try:
                for i in range(40):
                    logger.info(f"轮转测试第 {i} 行，填充到超过阈值")
                rotated = logger.LOG_DIR / "bridge.log.1"
                check("超过阈值会轮转", rotated.exists(), lambda v: v is True)
                tail = run_log.read_text(encoding="utf-8")
                check("轮转后触发行不丢", "轮转测试第 39 行", lambda s: s in tail, tail[-80:])
                check("轮转后仍在写新文件", len(tail.splitlines()), lambda n: n >= 1, str(len(tail.splitlines())))
            finally:
                logger.MAX_BYTES = orig_max
        finally:
            logger.LOG_DIR = orig_dir
            logger._handles = orig_handles

    print()
    if FAILURES:
        print(f"日志用例失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("日志用例全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
