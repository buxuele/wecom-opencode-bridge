import re

TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
HTML_TAG = re.compile(r"<[^>\n]+>")
FENCE = "```"
TRUNC_HINT = "…（输出过长已截断，剩余部分下一条继续推送）"


def split_table_row(line):
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def convert_table(lines):
    rows = []
    for line in lines:
        if TABLE_SEP.match(line):
            continue
        rows.append(split_table_row(line))
    if not rows:
        return []
    out = []
    header = rows[0]
    for idx, row in enumerate(rows[1:]):
        title = header[0] if header else "项"
        name = row[0] if row else ""
        if idx == 0:
            out.append(f"**{title}**")
        pairs = []
        for col_idx in range(1, len(row)):
            label = header[col_idx] if col_idx < len(header) else f"列{col_idx}"
            pairs.append(f"{label}: {row[col_idx]}")
        if not pairs:
            out.append(f"- {name}")
            continue
        out.append(f"- **{name}**")
        out.extend(f"  - {p}" for p in pairs)
    return out


def escape_html(text):
    def repl(m):
        return "&lt;" + m.group(0)[1:-1].replace("<", "&lt;").replace(">", "&gt;") + "&gt;"

    return HTML_TAG.sub(repl, text)


def _close_fence(text):
    if text.count(FENCE) % 2 == 0:
        return text
    return text + "\n" + FENCE


def _truncate(body, max_chars):
    keep = max(1, max_chars - len(TRUNC_HINT) - 8)
    head = body[:keep]
    head = _close_fence(head)
    return head + TRUNC_HINT


def render_raw(text):
    if text is None:
        return "（无输出）"
    body = str(text)
    body = escape_html(body)

    lines = body.split("\n")
    out = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if "|" in line and idx + 1 < len(lines) and TABLE_SEP.match(lines[idx + 1]):
            block = [line, lines[idx + 1]]
            idx += 2
            while idx < len(lines) and "|" in lines[idx]:
                block.append(lines[idx])
                idx += 1
            out.extend(convert_table(block))
            continue
        out.append(line)
        idx += 1

    result = "\n".join(out).strip()
    result = re.sub(r"\n{3,}", "\n\n", result)
    if not result:
        return "（无输出）"
    return result


def render(text, max_chars=3000, robot_name=""):
    if robot_name:
        text = strip_at_mention(text, robot_name)
    result = render_raw(text)
    if len(result) <= max_chars:
        return result
    return _truncate(result, max_chars)


def strip_at_mention(text, robot_name=""):
    if not text:
        return text
    if robot_name:
        pattern = re.compile(r"@" + re.escape(robot_name) + r"[ \u3000]*")
        return pattern.sub("", text).lstrip()
    return re.sub(r"^@\S+[ \u3000]*", "", text).lstrip()
