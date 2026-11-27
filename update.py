#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Append the latest A-share close into the embedded dataset of index.html.

The page ships with a baked-in history.  This script refreshes that history in
the cloud (GitHub Actions) so the browser has less work to do:

  1. read index.html, pull out the JSON object after ``const D = ``
  2. ask Eastmoney for the latest quotes (SSE / SZSE turnover, 588000, 512800)
     and for the Beijing Stock Exchange turnover (sum of all 920xxx names)
  3. append (or overwrite) the last bar, recompute MA20 and every pivot
  4. write index.html back

Everything is standard library only, so the workflow needs no pip install.
"""

import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "index.html")
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


# ---------------------------------------------------------------- helpers
def fetch_json(url, tries=3, wait=3):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - retry on anything network-ish
            last = exc
            if attempt + 1 < tries:
                time.sleep(wait)
    raise RuntimeError(f"GET failed: {url} ({last})")


def r2(x):
    return None if x is None else round(float(x) + 0.0, 2)


def r3(x):
    return None if x is None else round(float(x) + 0.0, 3)


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (a / b - 1.0) * 100.0


# ---------------------------------------------------------------- quotes
def fetch_quotes():
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get"
           "?secids=1.000001,0.399106,1.588000,1.512800"
           "&fields=f2,f3,f6,f12,f14,f124&fltt=2&invt=2")
    data = fetch_json(url).get("data") or {}
    rows = data.get("diff") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    out = {}
    for row in rows:
        code = str(row.get("f12"))
        out[code] = row
    need = ["000001", "399106", "588000", "512800"]
    missing = [c for c in need if c not in out]
    if missing:
        raise RuntimeError(f"quote payload missing {missing}: {rows}")
    return out


def fetch_bse():
    """Sum today's turnover (yuan) of every Beijing Stock Exchange listing."""
    total, count = 0.0, 0
    for page in range(1, 6):
        url = ("https://push2.eastmoney.com/api/qt/clist/get"
               f"?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
               "&fs=m:0+t:81+s:2048&fields=f12,f6")
        data = fetch_json(url).get("data") or {}
        rows = data.get("diff") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        if not rows:
            break
        for row in rows:
            code = str(row.get("f12") or "")
            if not code.startswith("920"):
                continue
            try:
                amount = float(row.get("f6"))
            except (TypeError, ValueError):
                continue  # suspended names come back as "-"
            total += amount
            count += 1
        if len(rows) < 100:
            break
        time.sleep(0.4)
    if count == 0:
        raise RuntimeError("BSE list returned no rows")
    return total, count


def fetch_kline(secid, beg, end):
    """Daily bars in yuan; used only to heal a gap when the job skipped days."""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1"
           f"&beg={beg}&end={end}")
    data = fetch_json(url, tries=1).get("data") or {}
    rows = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 7:
            continue
        rows.append({
            "date": p[0],
            "close": float(p[2]),
            "amount": float(p[6]),
        })
    if not rows:
        raise RuntimeError(f"empty kline for {secid}")
    return rows


# ---------------------------------------------------------------- pivots
def zigzag(values, threshold):
    n = len(values)
    if n < 2:
        return []
    pivots = []
    ext_i, ext, direction = 0, values[0], 0
    for i in range(1, n):
        v = values[i]
        if direction == 0:
            if v >= ext * (1.0 + threshold):
                direction = 1
                pivots.append(("L", ext_i))
                ext_i, ext = i, v
            elif v <= ext * (1.0 - threshold):
                direction = -1
                pivots.append(("H", ext_i))
                ext_i, ext = i, v
            elif v > ext:
                ext_i, ext = i, v
        elif direction == 1:
            if v > ext:
                ext_i, ext = i, v
            elif v <= ext * (1.0 - threshold):
                pivots.append(("H", ext_i))
                direction = -1
                ext_i, ext = i, v
        else:
            if v < ext:
                ext_i, ext = i, v
            elif v >= ext * (1.0 + threshold):
                pivots.append(("L", ext_i))
                direction = 1
                ext_i, ext = i, v
    pivots.append(("H" if direction == 1 else "L", ext_i))
    return pivots


def snap_to_raw(values, pivots, window=7):
    n = len(values)
    out = []
    for typ, i in pivots:
        lo = max(0, i - window)
        hi = min(n - 1, i + window)
        if typ == "H":
            j = max(range(lo, hi + 1), key=lambda k: values[k])
        else:
            j = min(range(lo, hi + 1), key=lambda k: values[k])
        out.append((typ, j, i - j))
    for k in range(1, len(out)):
        if out[k][1] <= out[k - 1][1]:
            typ, _, _off = out[k]
            out[k] = (typ, pivots[k][1], 0)
    return out


def detect_pivots(dates, raw, threshold):
    ma5 = []
    for i in range(len(raw)):
        lo = max(0, i - 2)
        hi = min(len(raw), i + 3)
        ma5.append(sum(raw[lo:hi]) / (hi - lo))
    pivots = snap_to_raw(raw, zigzag(ma5, threshold))
    result = []
    for k, (typ, idx, off) in enumerate(pivots):
        result.append({
            "type": typ,
            "idx": idx,
            "date": dates[idx],
            "value": raw[idx],
            "provisional": k == len(pivots) - 1,
            "ma_offset": off,
        })
    return result


def nearest_match(p, candidates):
    same = [x for x in candidates
            if x["type"] == p["type"] and abs(x["idx"] - p["idx"]) <= 20]
    if not same:
        return None
    return min(same, key=lambda x: (abs(x["idx"] - p["idx"]), x["idx"]))


def rebuild_pivots(dates, total, kc_price, bank_price):
    n = len(dates)
    turnover = detect_pivots(dates, total, 0.40)
    kc_pivots = detect_pivots(dates, kc_price, 0.10)
    bank_pivots = detect_pivots(dates, bank_price, 0.08)

    for p in turnover:
        p["chg20_before"] = pct(total[p["idx"]], total[p["idx"] - 20]) if p["idx"] >= 20 else None
        p["chg20_after"] = pct(total[p["idx"] + 20], total[p["idx"]]) if p["idx"] + 20 < n else None
        p["kc_fwd20"] = pct(kc_price[p["idx"] + 20], kc_price[p["idx"]]) if p["idx"] + 20 < n else None
        p["bank_fwd20"] = pct(bank_price[p["idx"] + 20], bank_price[p["idx"]]) if p["idx"] + 20 < n else None
        for key, pivots, prices in (("kc", kc_pivots, kc_price), ("bank", bank_pivots, bank_price)):
            m = nearest_match(p, pivots)
            if m:
                lag = m["idx"] - p["idx"]
                if lag == 0:
                    cls, desc = "同步", "同日"
                elif abs(lag) <= 5:
                    cls = "同步"
                    desc = ("量能领先" if lag > 0 else "ETF领先") + f" {abs(lag)} 日"
                else:
                    cls = "领先/滞后"
                    desc = ("量能领先" if lag > 0 else "ETF领先") + f" {abs(lag)} 日"
                p[key] = {
                    "date": m["date"], "idx": m["idx"], "lag": lag,
                    "value": prices[m["idx"]], "cls": cls, "desc": desc,
                }
            else:
                p[key] = None
    return turnover, kc_pivots, bank_pivots


def rebuild_meta(D, total, bse):
    n = len(D["dates"])
    pivots = D["pivots"]
    kc_matches = [p for p in pivots if p.get("kc")]
    bank_matches = [p for p in pivots if p.get("bank")]
    kc_lags = [abs(p["kc"]["lag"]) for p in kc_matches]
    bank_lags = [abs(p["bank"]["lag"]) for p in bank_matches]
    max_i = max(range(n), key=lambda i: total[i])
    min_i = min(range(n), key=lambda i: total[i])
    meta = D["meta"]
    meta.update({
        "days": n,
        "start": D["dates"][0],
        "end": D["dates"][-1],
        "mean": statistics.mean(total),
        "median": statistics.median(total),
        "max": total[max_i],
        "max_date": D["dates"][max_i],
        "min": total[min_i],
        "min_date": D["dates"][min_i],
        "today": total[-1],
        "today_prev": total[-2],
        "today_chg": pct(total[-1], total[-2]),
        "pivot_count": len(pivots),
        "kc_pivot_count": len(D["kc_pivots"]),
        "bank_pivot_count": len(D["bank_pivots"]),
        "kc_match_count": len(kc_matches),
        "bank_match_count": len(bank_matches),
        "kc_median_lag": statistics.median(kc_lags) if kc_lags else None,
        "bank_median_lag": statistics.median(bank_lags) if bank_lags else None,
        "kc_start": D["kc"][0],
        "kc_end": D["kc"][-1],
        "kc_return": pct(D["kc"][-1], D["kc"][0]),
        "bank_start": D["bank"][0],
        "bank_end": D["bank"][-1],
        "bank_return": pct(D["bank"][-1], D["bank"][0]),
        "bse_today": bse[-1],
    })
    meta.pop("user_today", None)
    meta.pop("user_diff", None)
    meta.pop("user_diff_pct", None)
    return meta


def recompute_ma20(total):
    out = []
    for i in range(len(total)):
        lo = max(0, i - 19)
        out.append(sum(total[lo:i + 1]) / (i + 1 - lo))
    return out


# ---------------------------------------------------------------- main
def load_page():
    with open(HTML, encoding="utf-8") as f:
        html = f.read()
    marker = "const D = "
    pos = html.index(marker) + len(marker)
    obj, end = json.JSONDecoder().raw_decode(html, pos)
    return html, pos, end, obj


def save_page(html, pos, end, D):
    blob = json.dumps(D, ensure_ascii=False, separators=(",", ":"))
    with open(HTML, "w", encoding="utf-8", newline="") as f:
        f.write(html[:pos] + blob + html[end:])


def add_bar(D, date, sh, sz, bse, kc_close, kc_pct, bank_close, bank_pct):
    D["dates"].append(date)
    D["sh"].append(r2(sh))
    D["sz"].append(r2(sz))
    D["bse"].append(r2(bse))
    D["total"].append(r2(sh + sz + bse))
    D["kc"].append(r3(kc_close))
    D["bank"].append(r3(bank_close))
    D["kc_pct"].append(r2(kc_pct))
    D["bank_pct"].append(r2(bank_pct))
    D["kc_norm"].append(r2(kc_close / D["kc"][0] * 100.0))
    D["bank_norm"].append(r2(bank_close / D["bank"][0] * 100.0))


def overwrite_last(D, sh, sz, bse, kc_close, kc_pct, bank_close, bank_pct):
    D["sh"][-1] = r2(sh)
    D["sz"][-1] = r2(sz)
    D["bse"][-1] = r2(bse)
    D["total"][-1] = r2(sh + sz + bse)
    D["kc"][-1] = r3(kc_close)
    D["bank"][-1] = r3(bank_close)
    D["kc_pct"][-1] = r2(kc_pct)
    D["bank_pct"][-1] = r2(bank_pct)
    D["kc_norm"][-1] = r2(kc_close / D["kc"][0] * 100.0)
    D["bank_norm"][-1] = r2(bank_close / D["bank"][0] * 100.0)


def main():
    force_date = os.environ.get("FORCE_DATE")
    html, pos, end, D = load_page()
    last = D["dates"][-1]

    quotes = fetch_quotes()
    idx, etf_kc, etf_bank = quotes["000001"], quotes["588000"], quotes["512800"]
    stamp = int(idx.get("f124") or 0)
    if force_date:
        bar_date = force_date
    elif stamp:
        bar_date = datetime.fromtimestamp(stamp, CST).strftime("%Y-%m-%d")
    else:
        bar_date = datetime.now(CST).strftime("%Y-%m-%d")

    if bar_date < last:
        print(f"quote date {bar_date} is older than baseline {last}; nothing to do")
        return 0
    if bar_date == last and not force_date:
        print(f"baseline already ends at {last}; refreshing the last bar only")

    sh = float(idx["f6"]) / 1e8
    sz = float(quotes["399106"]["f6"]) / 1e8
    bse, bse_count = fetch_bse()
    bse = bse / 1e8
    kc_close, kc_pct = float(etf_kc["f2"]), float(etf_kc["f3"])
    bank_close, bank_pct = float(etf_bank["f2"]), float(etf_bank["f3"])
    print(f"{bar_date}: sh={sh:.2f} sz={sz:.2f} bse={bse:.2f} kc={kc_close} bank={bank_close}")

    filled = 0
    if bar_date > last:
        gap = [d for d in [last] if False]
        # heal a gap by pulling daily bars, then append today's quote
        try:
            lookback = (datetime.strptime(bar_date, "%Y-%m-%d") - timedelta(days=20)).strftime("%Y%m%d")
            kl_sh = fetch_kline("1.000001", lookback, bar_date.replace("-", ""))
            kl_sz = fetch_kline("0.399106", lookback, bar_date.replace("-", ""))
            kl_kc = fetch_kline("1.588000", lookback, bar_date.replace("-", ""))
            kl_bank = fetch_kline("1.512800", lookback, bar_date.replace("-", ""))
            sh_map = {r["date"]: r["amount"] for r in kl_sh}
            sz_map = {r["date"]: r["amount"] for r in kl_sz}
            kc_map = {r["date"]: r["close"] for r in kl_kc}
            bank_map = {r["date"]: r["close"] for r in kl_bank}
            for d in sorted(sh_map):
                if d <= last or d >= bar_date:
                    continue
                prev_total = D["total"][-1]
                hi = (sh_map[d] + sz_map[d]) / 1e8
                prev_hi = (D["sh"][-1] + D["sz"][-1])
                ratio = (hi / prev_hi) if prev_hi else 1.0
                bse_est = D["bse"][-1] * ratio
                kc_p = kc_map[d] / 1e8 if kc_map[d] > 1e6 else kc_map[d]
                bank_p = bank_map[d] / 1e8 if bank_map[d] > 1e6 else bank_map[d]
                kc_prev = D["kc"][-1]
                bank_prev = D["bank"][-1]
                add_bar(D, d, sh_map[d] / 1e8, sz_map[d] / 1e8, bse_est,
                        kc_p, (kc_p / kc_prev - 1) * 100,
                        bank_p, (bank_p / bank_prev - 1) * 100)
                filled += 1
                _ = prev_total
        except Exception as exc:  # noqa: BLE001 - gap healing is best effort
            print(f"gap heal skipped: {exc}")
        add_bar(D, bar_date, sh, sz, bse, kc_close, kc_pct, bank_close, bank_pct)
    else:
        overwrite_last(D, sh, sz, bse, kc_close, kc_pct, bank_close, bank_pct)

    total = [float(x) for x in D["total"]]
    kc_price = [float(x) for x in D["kc"]]
    bank_price = [float(x) for x in D["bank"]]
    ma20 = recompute_ma20(total)
    D["ma20"] = [r2(x) for x in ma20]
    D["pivots"], D["kc_pivots"], D["bank_pivots"] = rebuild_pivots(
        D["dates"], total, kc_price, bank_price)
    rebuild_meta(D, total, [float(x) for x in D["bse"]])

    save_page(html, pos, end, D)
    print(f"wrote {HTML}: days={len(D['dates'])} last={D['dates'][-1]} "
          f"pivots={len(D['pivots'])} filled={filled} bse_names={bse_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
