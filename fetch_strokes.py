#!/usr/bin/env python3
import urllib.parse
"""下载 hanzi-writer-data 笔顺数据自托管（300 字 + 多音字补充）

用法: python3 fetch_strokes.py
"""
import csv
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).parent
CSV = ROOT / "data/parsed/hanzi_books.csv"
OUT = ROOT / "app/static/strokes"
CDN = "https://cdn.jsdelivr.net/npm/hanzi-writer-data@2.0/{}.json"


def fetch(ch):
    dst = OUT / f"{ch}.json"
    if dst.exists() and dst.stat().st_size > 100:
        return ch, True, "cached"
    try:
        with urllib.request.urlopen(CDN.format(urllib.parse.quote(ch)), timeout=20) as r:
            data = r.read()
        j = json.loads(data)
        assert j.get("strokes") and len(j["strokes"]) >= 1
        dst.write_bytes(data)
        return ch, True, f"{len(j['strokes'])}画"
    except Exception as e:
        return ch, False, str(e)[:80]


def main():
    import urllib.parse  # noqa
    chars = []
    with CSV.open() as f:
        for row in csv.DictReader(f):
            if row["char"] not in chars:
                chars.append(row["char"])
    OUT.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    with ThreadPoolExecutor(16) as ex:
        for ch, good, msg in ex.map(fetch, chars):
            ok, fail = ok + good, fail + (not good)
            if not good:
                print("FAIL", ch, msg)
    print(f"笔顺: {ok} ok / {fail} fail / {len(chars)} 字")
    # 抽样校验数据结构
    for ch in ("天", "我", "鸟"):
        j = json.loads((OUT / f"{ch}.json").read_text())
        print(ch, j["strokes"] and f"{len(j['strokes'])}画 medians={len(j['medians'])}", end="; ")
    print()


if __name__ == "__main__":
    main()
