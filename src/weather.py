"""日期与天气：Open-Meteo 免费接口，无需密钥。

地点优先取配置的城市名，配置为空时用 IP 定位兜底。
format_report 与 code_to_zh 是纯函数，网络只发生在 fetch 里。
"""

import os
import time
from datetime import datetime

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
IP_URL = "http://ip-api.com/json/"
TIMEOUT = 12.0

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

WMO_ZH = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "浓毛毛雨",
    56: "冻毛毛雨",
    57: "浓冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "大阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴小冰雹",
    99: "雷阵雨伴大冰雹",
}


def code_to_zh(code):
    try:
        return WMO_ZH.get(int(code), f"未知天气代码 {code}")
    except (TypeError, ValueError):
        return f"未知天气代码 {code}"


def _env_http_proxy():
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.getenv(name)
        if value and value.startswith("http"):
            return value
    return None


def _request_json(url, params=None):
    """先直连，失败再走环境里的 http 代理。不开 trust_env 是为了跳过 socks 代理。"""
    try:
        resp = httpx.get(url, params=params, timeout=TIMEOUT, trust_env=False)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        proxy = _env_http_proxy()
        if not proxy:
            raise
        resp = httpx.get(
            url, params=params, timeout=TIMEOUT, trust_env=False, proxy=proxy
        )
        resp.raise_for_status()
        return resp.json()


def geocode(city):
    body = _request_json(
        GEOCODE_URL, {"name": city, "count": 1, "language": "zh", "format": "json"}
    )
    results = body.get("results") or []
    if not results:
        raise LookupError(f"没查到城市「{city}」，换个写法再试")
    hit = results[0]
    return {
        "name": hit.get("name") or city,
        "latitude": hit["latitude"],
        "longitude": hit["longitude"],
        "admin1": hit.get("admin1") or "",
        "country": hit.get("country") or "",
    }


def locate_by_ip():
    body = _request_json(IP_URL, {"fields": "status,country,regionName,city,lat,lon", "lang": "zh-CN"})
    if body.get("status") != "success":
        raise LookupError("IP 定位失败，请在 .env 里设置 WECOM_WEATHER_CITY 指定城市")
    city = body.get("city") or body.get("regionName") or "未知城市"
    return {
        "name": city,
        "latitude": body["lat"],
        "longitude": body["lon"],
        "admin1": body.get("regionName") or "",
        "country": body.get("country") or "",
    }


def forecast(latitude, longitude):
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
        "precipitation,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code",
        "timezone": "auto",
        "forecast_days": 1,
    }
    body = _request_json(FORECAST_URL, params)
    return body.get("current") or {}, body.get("daily") or {}


def _first(values):
    if isinstance(values, list) and values:
        return values[0]
    return None


def format_report(city, current, daily, now=None):
    moment = datetime.fromtimestamp(now if now is not None else time.time())
    lines = ["**日期与天气**", ""]
    lines.append(
        f"日期：{moment.strftime('%Y-%m-%d')} {WEEKDAYS[moment.weekday()]} "
        f"{moment.strftime('%H:%M')}"
    )
    lines.append(f"城市：{city}")
    if current:
        temperature = current.get("temperature_2m")
        feels = current.get("apparent_temperature")
        desc = code_to_zh(current.get("weather_code"))
        lines.append(f"天气：{desc}，气温 {temperature} 度，体感 {feels} 度")
        humidity = current.get("relative_humidity_2m")
        wind = current.get("wind_speed_10m")
        rain = current.get("precipitation")
        lines.append(f"湿度 {humidity}%，风速 {wind} 公里每小时，降水 {rain} 毫米")
    high = _first(daily.get("temperature_2m_max")) if daily else None
    low = _first(daily.get("temperature_2m_min")) if daily else None
    if high is not None and low is not None:
        lines.append(f"气温：最低 {low} 度，最高 {high} 度")
    if not current and high is None:
        lines.append("天气数据为空。")
    return "\n".join(lines)


def fetch(city="", now=None, ip_fallback=False):
    city = (city or "").strip()
    if city:
        place = geocode(city)
    elif ip_fallback:
        place = locate_by_ip()
    else:
        raise LookupError(
            "未配置城市。请在 .env 的 WECOM_WEATHER_CITY 填城市名，"
            "或设 WECOM_WEATHER_IP_FALLBACK=1 用 IP 定位兜底"
        )
    current, daily = forecast(place["latitude"], place["longitude"])
    return format_report(place["name"], current, daily, now=now)
