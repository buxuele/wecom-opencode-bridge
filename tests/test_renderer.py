import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import renderer

FAILURES = []


def check(name, actual, predicate, detail=""):
    ok = predicate(actual)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def is_table_free(text):
    lines = [l for l in text.split("\n") if "|" in l and "---" in l]
    return not lines


def main():
    md = """| 名称 | 说明 | 默认 |
| --- | --- | --- |
| a | 第一 | x |
| b | 第二 | y |"""
    out = renderer.render(md)
    check("表格转列表", out, is_table_free, out)
    check("表格保留内容", out, lambda t: "第一" in t and "第二" in t, out)

    html = "<div>hello</div>"
    out = renderer.render(html)
    check("HTML 转义", out, lambda t: "<div>" not in t, out)

    long_text = "x" * 5000
    out = renderer.render(long_text, max_chars=500)
    long_out = renderer.render(long_text, max_chars=500)
    check("超长截断", len(long_out), lambda n: n <= 500, f"实际 {len(long_out)}")
    check("截断有提示", renderer.render(long_text, max_chars=500), lambda t: "截断" in t)

    fenced = "```python\nprint(1)\n" + "y" * 2000
    cut = renderer.render(fenced, max_chars=500)
    check("截断后围栏闭合", cut, lambda t: t.count("```") % 2 == 0, cut[-80:])

    out = renderer.render("short one line")
    check("短文本原样", out, lambda t: t == "short one line", out)

    code = "line1\nline2\nline3\nline4"
    out = renderer.render(code, max_chars=100000)
    check("多行保留", out, lambda t: "\n" in t, out)

    out = renderer.render("")
    check("空输出兜底", out, lambda t: t == "（无输出）", out)

    out = renderer.render("@MyBot 帮我改代码", robot_name="MyBot")
    check("剥离@提及", out, lambda t: "@MyBot" not in t and "帮我改代码" in t, out)

    out = renderer.strip_at_mention("@某机器人 你好", "")
    check("无名称时通用剥离", out, lambda t: t == "你好", out)

    out = renderer.render_raw("")
    check("空输出占位", out, lambda t: t == "（无输出）", out)

    messy = "a\n\n\n\n\nb"
    out = renderer.render(messy)
    check("压缩空行", out, lambda t: "\n\n\n" not in t, out)

    print()
    if FAILURES:
        print(f"渲染器用例失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("渲染器全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())