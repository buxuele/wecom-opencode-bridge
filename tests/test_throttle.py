import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import throttle

FAILURES = []


def check(name, actual, predicate, detail=""):
    ok = predicate(actual)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def main():
    clk = FakeClock()
    th = throttle.Throttle(min_chars=300, min_interval=2.0, clock=clk)

    check("首次立即可发", th.ready(10), lambda v: v is True)
    th.mark(10)

    check("字符不足不发", th.ready(50), lambda v: v is False)
    clk.advance(1.0)
    check("时间不足不发", th.ready(50), lambda v: v is False)

    check("字符达标发", th.ready(400), lambda v: v is True)
    th.mark(400)

    clk.advance(1.5)
    check("时间未到且字符不足不发", th.ready(450), lambda v: v is False)

    clk.advance(1.0)
    check("时间超出发", th.ready(450), lambda v: v is True)

    th.reset()
    check("重置后可发", th.ready(999), lambda v: v is True)

    chunks = throttle.chunk_text("a" * 5000, max_chars=1800)
    check("长文本切块", chunks, lambda c: len(c) == 3, f"得到 {len(chunks)} 块")
    check("每块不超限", chunks, lambda c: all(len(x) <= 1800 for x in c))
    check("拼接还原", "".join(chunks), lambda s: s == "a" * 5000)

    check("短文本单块", throttle.chunk_text("short", 1800), lambda c: c == ["short"])

    print()
    if FAILURES:
        print(f"节流器用例失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("节流器全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())