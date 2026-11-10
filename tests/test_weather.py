import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import weather

FAILURES = []
NOW = 1_800_000_000.0


def check(name, actual, predicate, detail=""):
    ok = predicate(actual)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def main():
    check("晴", weather.code_to_zh(0), lambda t: t == "晴")
    check("小雨", weather.code_to_zh(61), lambda t: t == "小雨")
    check("字符串代码也认", weather.code_to_zh("95"), lambda t: t == "雷阵雨")
    check("未知代码不崩", weather.code_to_zh(999), lambda t: "未知" in t)
    check("非法代码不崩", weather.code_to_zh(None), lambda t: "未知" in t)

    current = {
        "temperature_2m": 29.1,
        "apparent_temperature": 32.0,
        "relative_humidity_2m": 64,
        "wind_speed_10m": 12.3,
        "precipitation": 0.0,
        "weather_code": 2,
    }
    daily = {"temperature_2m_max": [31.0], "temperature_2m_min": [22.0]}

    report = weather.format_report("上海", current, daily, now=NOW)
    check("含标题", report, lambda t: "日期与天气" in t)
    check("含城市", report, lambda t: "上海" in t)
    check("含天气描述", report, lambda t: "局部多云" in t)
    check("含体感温度", report, lambda t: "体感 32.0 度" in t)
    check("含最低气温", report, lambda t: "最低 22.0 度" in t)
    check("含最高气温", report, lambda t: "最高 31.0 度" in t)
    check("含湿度", report, lambda t: "湿度 64%" in t)
    check("含日期行", report, lambda t: "日期：" in t)

    empty = weather.format_report("某地", {}, {}, now=NOW)
    check("空数据有兜底", empty, lambda t: "天气数据为空" in t)

    try:
        weather.fetch("")
        check("空城市且无兜底应报错", "未抛出", lambda v: False)
    except LookupError as exc:
        check("空城市且无兜底应报错", "未配置城市" in str(exc), lambda v: v is True)
    except Exception as exc:
        check("空城市且无兜底应报错", f"意外异常 {type(exc).__name__}", lambda v: False)

    print()
    if FAILURES:
        print(f"天气用例失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("天气全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
