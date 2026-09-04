#!/usr/bin/env python3
"""Screen Shanghai/Shenzhen main-board stocks for limit-up gene + bottom volume.

The script uses only the Python standard library and writes an auditable JSON
package suitable for a workbook builder. Public endpoints can change or rate
limit; caches are retained under the caller-selected cache directory.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import math
import os
import statistics
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo


MOMA_BASE = "https://api.momaapi.com/hslt"
MOMA_DEMO_TOKEN = "TEST-API-TOKEN-MOMA-836089C22111"
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}


def parse_args():
    now = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    default_end = now.date() if now.hour >= 18 else now.date() - dt.timedelta(days=1)
    p = argparse.ArgumentParser(description="A股主板涨停基因×底部放量交叉筛选")
    p.add_argument("--end", default=default_end.isoformat(), help="requested cutoff YYYY-MM-DD")
    p.add_argument("--output", help="output JSON path")
    p.add_argument("--self-test", action="store_true", help="run deterministic offline checks")
    p.add_argument("--cache-dir", default="work/limit-up-gene-cache")
    p.add_argument("--moma-token", default=os.environ.get("MOMA_API_TOKEN", MOMA_DEMO_TOKEN))
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--fast-cutoff", default="10:00")
    p.add_argument("--min-limit-ups", type=int, default=5)
    p.add_argument("--min-gene-score", type=float, default=55)
    p.add_argument("--min-shape-score", type=float, default=50)
    p.add_argument("--min-seal-rate", type=float, default=0.65)
    p.add_argument("--min-match-rate", type=float, default=0.90)
    return p.parse_args()


def subtract_months(value: dt.date, months: int) -> dt.date:
    year = value.year
    month = value.month - months
    while month <= 0:
        year -= 1
        month += 12
    month_days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                  31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return dt.date(year, month, min(value.day, month_days[month - 1]))


def fetch_json(url: str, timeout=25, retries=4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url}: {last}")


def is_main_board(code: str) -> bool:
    return code.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))


def excluded_name(name: str) -> bool:
    value = (name or "").upper()
    return "ST" in value or "退" in value


def clean_name(name: str) -> str:
    return "".join((name or "").split())


def expand_industry(value: str) -> str:
    return {
        "旅游及景": "旅游及景区", "房地产开": "房地产开发",
        "汽车零部": "汽车零部件", "炼化及贸": "炼化及贸易",
        "金属新材": "金属新材料", "IT服务Ⅱ": "IT服务",
    }.get(value, value)


def fstr(row, *keys, default=""):
    for key in keys:
        if key in row and row[key] is not None:
            return str(row[key])
    return default


def fnum(row, *keys, default=0.0):
    for key in keys:
        value = row.get(key)
        if value not in (None, "", "-"):
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return default


def fint(row, *keys, default=0):
    return int(round(fnum(row, *keys, default=default)))


def ratio(a, b, default=0.0):
    return a / b if b else default


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def safe_mean(values):
    values = [x for x in values if x is not None and math.isfinite(x)]
    return statistics.mean(values) if values else 0.0


def parse_clock(value) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace(":", "")
    if text.endswith(".0"):
        text = text[:-2]
    text = text.zfill(6)
    if len(text) != 6 or not text.isdigit():
        return None
    h, m, s = int(text[:2]), int(text[2:4]), int(text[4:6])
    return h * 3600 + m * 60 + s if h < 24 and m < 60 and s < 60 else None


def cutoff_seconds(value: str) -> int:
    parsed = parse_clock(value)
    if parsed is None:
        raise ValueError(f"invalid time: {value}")
    return parsed


def format_clock(seconds: int | None) -> str:
    if seconds is None:
        return ""
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def parse_streak(row) -> int:
    direct = fint(row, "lbc", "continue_day_cnt")
    if direct:
        return direct
    text = fstr(row, "tj", "high_days")
    try:
        return int(text.split("/")[-1].replace("板", "")) if "/" in text else 0
    except ValueError:
        return 0


def tencent_url(code: str, start: dt.date, end: dt.date) -> str:
    symbol = ("sh" if code.startswith("6") else "sz") + code
    param = f"{symbol},day,{start.isoformat()},{end.isoformat()},320,qfq"
    return TENCENT_KLINE + "?param=" + param


def fetch_bars(code: str, start: dt.date, end: dt.date):
    raw = fetch_json(tencent_url(code, start, end))
    symbol = ("sh" if code.startswith("6") else "sz") + code
    data = (raw.get("data") or {}).get(symbol) or {}
    source = data.get("qfqday") or data.get("day") or []
    bars = []
    for row in source:
        if len(row) < 6:
            continue
        try:
            bars.append({
                "date": row[0], "open": float(row[1]), "close": float(row[2]),
                "high": float(row[3]), "low": float(row[4]), "volume": float(row[5]),
            })
        except (TypeError, ValueError):
            continue
    return code, bars


def fetch_pool(day: str, endpoint: str, token: str):
    url = f"{MOMA_BASE}/{endpoint}/{day}/{token}"
    try:
        data = fetch_json(url)
    except RuntimeError as exc:
        if "HTTP Error 404" in str(exc):
            data = []
        else:
            raise
    return day, endpoint, data if isinstance(data, list) else []


def collect_pools(trading_days, token, cache_dir: Path, workers: int):
    cache = cache_dir / f"pools_{min(trading_days)}_{max(trading_days)}.json"
    if cache.exists():
        return json.loads(cache.read_text("utf-8"))
    result = {"ztgc": {}, "zbgc": {}}
    tasks = [(day, endpoint) for day in sorted(trading_days) for endpoint in ("ztgc", "zbgc")]
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch_pool, day, endpoint, token) for day, endpoint in tasks]
        for index, future in enumerate(cf.as_completed(futures), 1):
            day, endpoint, rows = future.result()
            result[endpoint][day] = rows
            if index % 50 == 0:
                print(f"pool requests: {index}/{len(tasks)}", flush=True)
    cache.write_text(json.dumps(result, ensure_ascii=False), "utf-8")
    return result


def extract_events(pools, trading_days):
    output = {"sealed": [], "failed": []}
    for endpoint, result_name in (("ztgc", "sealed"), ("zbgc", "failed")):
        for day, rows in pools[endpoint].items():
            if day not in trading_days:
                continue
            for row in rows:
                code = fstr(row, "dm", "code", "c").replace("sh", "").replace("sz", "")
                name = clean_name(fstr(row, "mc", "name", "n"))
                if not is_main_board(code) or excluded_name(name):
                    continue
                output[result_name].append({
                    "date": day, "code": code, "name": name,
                    "industry": expand_industry(fstr(row, "hy", "hybk", "industry")),
                    "first_time_s": parse_clock(row.get("fbt", row.get("first_limit_time"))),
                    "last_time_s": parse_clock(row.get("lbt", row.get("last_limit_time"))),
                    "breaks": fint(row, "zbc", "break_limit_up_times"),
                    "streak": parse_streak(row), "turnover": fnum(row, "hs", "turnover_rate"),
                    "amount": fnum(row, "cje", "amount"), "float_cap": fnum(row, "lt", "ltsz"),
                    "price": fnum(row, "p", "price"), "pct": fnum(row, "zf", "zdp"),
                })
    for rows in output.values():
        rows.sort(key=lambda x: (x["date"], x["code"]))
    return output


def shape_metrics(bars, shape_start: dt.date):
    rows = [bar for bar in bars if bar["date"] >= shape_start.isoformat()]
    if len(rows) < 40:
        return None
    closes = [x["close"] for x in rows]
    lows = [x["low"] for x in rows]
    highs = [x["high"] for x in rows]
    volumes = [x["volume"] for x in rows]
    latest = rows[-1]
    low, high = min(lows), max(highs)
    position = ratio(latest["close"] - low, high - low, 0.5)
    v5 = safe_mean(volumes[-5:])
    vbase = safe_mean(volumes[-25:-5]) if len(volumes) >= 25 else safe_mean(volumes[:-5])
    volume_ratio = ratio(v5, vbase)
    active_days = sum(1 for v in volumes[-10:] if vbase and v >= 1.3 * vbase)
    ma5, ma10, ma20 = safe_mean(closes[-5:]), safe_mean(closes[-10:]), safe_mean(closes[-20:])
    ma20_prev = safe_mean(closes[-25:-5]) if len(closes) >= 25 else ma20
    slope = ratio(ma20, ma20_prev, 1) - 1
    ret20 = ratio(latest["close"], closes[-21], 1) - 1 if len(closes) >= 21 else 0
    bottom_score = 100 * clamp((0.55 - position) / 0.55)
    volume_score = 100 * clamp((volume_ratio - 1) / 1.2)
    persist_score = 100 * clamp(active_days / 4)
    trend_score = (35 if ma5 >= ma10 else 0) + (35 if latest["close"] >= ma20 else 0) + (30 if slope > 0 else 0)
    penalty = min(25, max(0, position - 0.55) * 50) + min(20, max(0, ret20 - 0.25) * 50)
    score = clamp(0.38 * bottom_score + 0.30 * volume_score + 0.17 * persist_score + 0.15 * trend_score - penalty, 0, 100)
    confirmed = position <= 0.50 and volume_ratio >= 1.20 and active_days >= 2 and (ma5 >= ma10 or latest["close"] >= ma20)
    strong = position <= 0.40 and volume_ratio >= 1.40 and active_days >= 2 and latest["close"] >= ma20
    status = "强确认" if strong else "确认" if confirmed else "观察" if position <= 0.55 and volume_ratio >= 1.05 else "不满足"
    return {
        "as_of": latest["date"], "trade_days_3m": len(rows), "close": latest["close"],
        "range_low": low, "range_high": high, "range_position": position,
        "above_low": ratio(latest["close"], low, 1) - 1,
        "vol_ratio_5_20": volume_ratio, "active_volume_days_10": active_days,
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma20_slope_20d": slope,
        "ret20": ret20, "shape_score": score, "shape_status": status,
    }


def build_gene_rows(events, end_date: dt.date, fast_cutoff: int):
    by_s, by_f = defaultdict(list), defaultdict(list)
    day_industry = defaultdict(Counter)
    for event in events["sealed"]:
        by_s[event["code"]].append(event)
        if event["industry"]:
            day_industry[event["date"]][event["industry"]] += 1
    for event in events["failed"]:
        by_f[event["code"]].append(event)
    result = []
    for code, sealed in by_s.items():
        failed = by_f[code]
        times = [x["first_time_s"] for x in sealed if x["first_time_s"] is not None]
        fast = sum(value <= fast_cutoff for value in times)
        early = sum(value <= 10 * 3600 + 30 * 60 for value in times)
        sector_days = sum(day_industry[x["date"]][x["industry"]] >= 3 for x in sealed if x["industry"])
        touches = len(sealed) + len(failed)
        name = Counter(x["name"] for x in sealed).most_common(1)[0][0]
        industry_counts = Counter(x["industry"] for x in sealed if x["industry"])
        industry = industry_counts.most_common(1)[0][0] if industry_counts else ""
        row = {
            "code": code, "name": name, "industry": industry,
            "limit_ups_6m": len(sealed), "failed_boards_6m": len(failed), "touches_6m": touches,
            "seal_success_rate": ratio(len(sealed), touches), "fast_count": fast,
            "fast_rate": ratio(fast, len(times)), "early_count_1030": early,
            "early_rate_1030": ratio(early, len(times)),
            "median_first_time": format_clock(int(statistics.median(times))) if times else "",
            "missing_first_time_count": len(sealed) - len(times),
            "one_word_count": sum(x["first_time_s"] == 9 * 3600 + 25 * 60 and x["breaks"] == 0 for x in sealed),
            "sector_hot_count": sector_days, "sector_link_rate": ratio(sector_days, len(sealed)),
            "max_streak": max((x["streak"] for x in sealed), default=0),
            "recent_30d_limit_ups": sum(x["date"] >= (end_date - dt.timedelta(days=30)).isoformat() for x in sealed),
            "avg_success_breaks": safe_mean([x["breaks"] for x in sealed]),
            "last_limit_date": max(x["date"] for x in sealed),
            "event_dates": [x["date"] for x in sealed],
        }
        raw = 100 * (0.35 * min(1, len(sealed) / 12) + 0.25 * row["seal_success_rate"] +
                     0.20 * row["fast_rate"] + 0.10 * row["sector_link_rate"] +
                     0.10 * min(1, row["recent_30d_limit_ups"] / 3))
        reliability = 0.75 + 0.25 * min(1, math.sqrt(touches / 6))
        row["gene_score"] = raw * reliability
        result.append(row)
    return result, by_s


def load_candidate_bars(codes, start, end, cache_dir: Path, workers):
    cache = cache_dir / f"bars_{start}_{end}.json"
    saved = json.loads(cache.read_text("utf-8")) if cache.exists() else {}
    missing = [code for code in codes if code not in saved]
    if missing:
        with cf.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch_bars, code, start, end) for code in missing]
            for index, future in enumerate(cf.as_completed(futures), 1):
                code, bars = future.result()
                saved[code] = bars
                if index % 50 == 0:
                    cache.write_text(json.dumps(saved, ensure_ascii=False), "utf-8")
                    print(f"daily bars: {index}/{len(missing)}", flush=True)
        cache.write_text(json.dumps(saved, ensure_ascii=False), "utf-8")
    return saved


def risk_flags(row):
    flags = []
    if row["seal_success_rate"] < 0.65:
        flags.append("封板成功率偏低")
    if row["fast_rate"] < 0.50:
        flags.append("快板占比一般")
    if row["sector_link_rate"] < 0.40:
        flags.append("行业联动偏弱")
    if row["range_position"] > 0.45:
        flags.append("已离开深底区")
    if row["ret20"] > 0.20:
        flags.append("近20日涨幅偏大")
    if row["vol_ratio_5_20"] > 3:
        flags.append("量能过热/一次性脉冲")
    if row["recent_30d_limit_ups"] == 0:
        flags.append("近30日无涨停触发")
    return "；".join(flags or ["事件驱动退潮风险"])


def run_self_test():
    assert parse_clock("09:25:00") == 33900
    assert parse_clock(100000) == 36000
    start = dt.date(2026, 1, 1)
    bars = []
    for index in range(65):
        price = 12 - 0.10 * index if index < 20 else 10 + max(0, index - 59) * 0.02
        volume = 2000 if index >= 60 else 1000
        bars.append({
            "date": (start + dt.timedelta(days=index)).isoformat(),
            "open": price, "high": price * 1.01, "low": price * 0.99,
            "close": price, "volume": volume,
        })
    metrics = shape_metrics(bars, start)
    assert metrics and metrics["vol_ratio_5_20"] == 2
    assert metrics["shape_status"] in ("确认", "强确认")
    print(json.dumps({"self_test": "OK", "shape_status": metrics["shape_status"],
                      "volume_ratio": metrics["vol_ratio_5_20"]}, ensure_ascii=False))


def main():
    args = parse_args()
    if args.self_test:
        run_self_test()
        return
    if not args.output:
        raise SystemExit("--output is required unless --self-test is used")
    requested_end = dt.date.fromisoformat(args.end)
    provisional_start = subtract_months(requested_end, 6) - dt.timedelta(days=5)
    cache_dir = Path(args.cache_dir).expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    _, calendar_bars = fetch_bars("000001", provisional_start, requested_end)
    if not calendar_bars:
        raise RuntimeError("无法取得交易日日线，停止以避免把休市日回退数据计入样本")
    actual_end = max(dt.date.fromisoformat(row["date"]) for row in calendar_bars)
    start_6m, start_3m = subtract_months(actual_end, 6), subtract_months(actual_end, 3)
    trading_days = {row["date"] for row in calendar_bars if row["date"] >= start_6m.isoformat()}
    pools = collect_pools(trading_days, args.moma_token, cache_dir, args.workers)
    events = extract_events(pools, trading_days)
    rows, sealed_by_code = build_gene_rows(events, actual_end, cutoff_seconds(args.fast_cutoff))

    broad = [row for row in rows if row["limit_ups_6m"] >= 3 and row["fast_count"] >= 1]
    bars_by_code = load_candidate_bars([row["code"] for row in broad], start_6m, actual_end, cache_dir, args.workers)
    combined = []
    for row in broad:
        bars = bars_by_code.get(row["code"], [])
        shape = shape_metrics(bars, start_3m)
        if not shape:
            continue
        row.update(shape)
        by_date = {bar["date"]: bar for bar in bars}
        previous = {bars[i]["date"]: bars[i - 1]["close"] for i in range(1, len(bars))}
        comparable = [x["date"] for x in sealed_by_code[row["code"]] if x["date"] in by_date and x["date"] in previous]
        matched = [day for day in comparable if by_date[day]["close"] / previous[day] - 1 >= 0.093]
        row["kline_comparable_events"] = len(comparable)
        row["kline_validated_events"] = len(matched)
        row["kline_match_rate"] = ratio(len(matched), len(comparable))
        row["combined_score"] = 0.58 * row["gene_score"] + 0.42 * row["shape_score"]
        row["grade"] = "A" if row["combined_score"] >= 68 else "B" if row["combined_score"] >= 60 else "C"
        row["risk_flags"] = risk_flags(row)
        combined.append(row)

    combined.sort(key=lambda x: x["combined_score"], reverse=True)
    final = [row for row in combined if row["limit_ups_6m"] >= args.min_limit_ups and
             row["gene_score"] >= args.min_gene_score and row["shape_score"] >= args.min_shape_score and
             row["seal_success_rate"] >= args.min_seal_rate and row["shape_status"] in ("确认", "强确认") and
             row["kline_match_rate"] >= args.min_match_rate]
    watch = [row for row in combined if row not in final and row["gene_score"] >= args.min_gene_score and
             row["shape_score"] >= 40 and row["kline_match_rate"] >= args.min_match_rate]

    output = {
        "metadata": {
            "requested_end": requested_end.isoformat(), "end_date": actual_end.isoformat(),
            "start_6m": start_6m.isoformat(), "start_3m": start_3m.isoformat(),
            "trading_day_count": len(trading_days), "success_event_count": len(events["sealed"]),
            "failed_event_count": len(events["failed"]), "candidate_count": len(broad),
            "final_count": len(final), "watch_count": len(watch), "fast_cutoff": args.fast_cutoff,
            "method_version": "1.0.0",
        },
        "thresholds": {
            "min_limit_ups": args.min_limit_ups, "min_gene_score": args.min_gene_score,
            "min_shape_score": args.min_shape_score, "min_seal_rate": args.min_seal_rate,
            "min_match_rate": args.min_match_rate,
        },
        "final": final, "watch": watch, "broad": combined,
        "success_events": events["sealed"], "failed_events": events["failed"],
        "klines": {code: {"bars": bars} for code, bars in bars_by_code.items()},
        "sources": {
            "MOMA-ZT": "https://momaapi.com/docs-shares.html#涨停股池",
            "MOMA-ZB": "https://momaapi.com/docs-shares.html#炸板股池",
            "TENCENT-D": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        },
    }
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(output["metadata"], ensure_ascii=False, indent=2))
    print(f"saved: {output_path}")


if __name__ == "__main__":
    main()
