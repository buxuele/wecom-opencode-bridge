"""快捷菜单：帮助触发词与编号选择。

菜单项是一份数据表，新增功能只需往 ITEMS 里加一条，编号会自动跟着变。
"""

TRIGGERS = {
    "help",
    "帮助",
    "?",
    "？",
    "菜单",
    "快捷菜单",
    "menu",
    "/help",
    "/帮助",
    "/菜单",
    "/menu",
}

ITEMS = [
    {
        "key": "activity",
        "title": "其他会话动态",
        "hint": "OpenCode 其他会话正在做什么",
    },
    {
        "key": "weather",
        "title": "日期与天气",
        "hint": "今天的日期、时间和所在城市天气",
    },
]


def is_trigger(text):
    return (text or "").strip().lower() in TRIGGERS


def selection_of(text):
    """把编号输入解析成 1 起的菜单序号，不是选择则返回 None。

    支持裸数字、菜单1、快捷菜单1 三种写法。数字超出范围一律返回 None，
    交给普通聊天处理，避免误伤用户随手发的数字。
    """
    body = (text or "").strip().lower()
    if not body:
        return None
    if body in TRIGGERS:
        return None
    for prefix in ("快捷菜单", "菜单"):
        if body.startswith(prefix):
            body = body[len(prefix):].strip()
            break
    if not body.isdigit():
        return None
    index = int(body)
    if 1 <= index <= len(ITEMS):
        return index
    return None


def item_at(index):
    if 1 <= index <= len(ITEMS):
        return ITEMS[index - 1]
    return None


def has_prefix(text):
    """是否用了显式前缀写法，例如 菜单2、快捷菜单2。显式写法不受时间窗限制。"""
    body = (text or "").strip().lower()
    return body.startswith("快捷菜单") or body.startswith("菜单")


def menu_text():
    lines = ["**快捷菜单**", ""]
    for index, item in enumerate(ITEMS, start=1):
        lines.append(f"{index}. {item['title']} —— {item['hint']}")
    lines.append("")
    lines.append("回复序号执行，其余文字会直接作为指令发给 OpenCode。")
    return "\n".join(lines)
