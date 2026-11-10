import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import quick_menu

FAILURES = []


def check(name, actual, predicate, detail=""):
    ok = predicate(actual)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def main():
    for trigger in ["help", "HELP", "帮助", "?", "？", "菜单", "/help", " menu "]:
        check(f"触发词 {trigger!r}", quick_menu.is_trigger(trigger), lambda r: r is True)

    for text in ["帮我 help 一下", "hello", "1a", "", "帮助我"]:
        check(f"非触发 {text!r}", quick_menu.is_trigger(text), lambda r: r is False)

    check("编号 1", quick_menu.selection_of("1"), lambda r: r == 1)
    check("编号带空格", quick_menu.selection_of(" 2 "), lambda r: r == 2)
    check("菜单前缀", quick_menu.selection_of("菜单2"), lambda r: r == 2)
    check("快捷菜单前缀", quick_menu.selection_of("快捷菜单1"), lambda r: r == 1)
    check("越界编号", quick_menu.selection_of("9"), lambda r: r is None)
    check("零不是选择", quick_menu.selection_of("0"), lambda r: r is None)
    check("普通文字不是选择", quick_menu.selection_of("帮我写代码"), lambda r: r is None)
    check("触发词不算选择", quick_menu.selection_of("help"), lambda r: r is None)

    menu = quick_menu.menu_text()
    check("菜单含第 1 项", menu, lambda t: "1." in t and quick_menu.ITEMS[0]["title"] in t)
    check("菜单含第 2 项", menu, lambda t: "2." in t and quick_menu.ITEMS[1]["title"] in t)

    check("item_at 取首项", quick_menu.item_at(1)["key"], lambda k: k == "activity")
    check("item_at 越界返回空", quick_menu.item_at(99), lambda v: v is None)
    check("前缀识别 菜单2", quick_menu.has_prefix("菜单2"), lambda v: v is True)
    check("前缀识别 快捷菜单1", quick_menu.has_prefix("快捷菜单1"), lambda v: v is True)
    check("裸数字不算前缀", quick_menu.has_prefix("2"), lambda v: v is False)

    print()
    if FAILURES:
        print(f"快捷菜单用例失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("快捷菜单全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
