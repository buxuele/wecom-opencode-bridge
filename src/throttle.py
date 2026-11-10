import time


class Throttle:
    def __init__(self, min_chars=300, min_interval=2.0, clock=time.monotonic):
        self.min_chars = min_chars
        self.min_interval = min_interval
        self.clock = clock
        self.last_sent = 0.0
        self.last_len = 0

    def reset(self):
        self.last_sent = 0.0
        self.last_len = 0

    def ready(self, total_len):
        if self.last_len == 0:
            return True
        grew = total_len - self.last_len
        if grew <= 0:
            return False
        elapsed = self.clock() - self.last_sent
        return grew >= self.min_chars or elapsed >= self.min_interval

    def mark(self, total_len):
        self.last_sent = self.clock()
        self.last_len = total_len

    def next_deadline(self):
        return self.min_interval


def chunk_text(text, max_chars=1800):
    if text is None:
        return []
    body = str(text)
    if len(body) <= max_chars:
        return [body]
    out = []
    current = []
    size = 0
    for line in body.split("\n"):
        piece = line if not current else "\n" + line
        if size + len(piece) > max_chars and current:
            out.append("".join(current))
            current = []
            size = 0
        if len(piece) > max_chars:
            if current:
                out.append("".join(current))
                current = []
                size = 0
            for start in range(0, len(piece), max_chars):
                out.append(piece[start : start + max_chars])
            continue
        current.append(piece)
        size += len(piece)
    if current:
        out.append("".join(current))
    return out