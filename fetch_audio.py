#!/usr/bin/env python3
"""edge-tts 批量生成音频（自托管，同 wordslearn audio seed 模式）

文件命名:
  - 每字主音 {char}.mp3，第二读音 {char}_{tone3}.mp3（如 长_zhang3.mp3）
  - e_{char}.mp3 字源故事 / w_{char}.mp3 组词（顿号连接）/ s_{char}.mp3 例句
  - 字音读单遍（"天。"），rate -20% 放慢；多音字按词境短语消歧
  - voice: zh-CN-XiaoxiaoNeural（活泼女声，适合儿童）

用法: python3 fetch_audio.py [--force]   # --force 无视缓存重生成
"""
import asyncio
import csv
import re
import json
import sys
from pathlib import Path

import edge_tts

ROOT = Path(__file__).parent
CSV = ROOT / "data/parsed/hanzi_books.csv"
CONTENT = ROOT / "data/parsed/zi_content.json"
OUT = ROOT / "app/static/audio"
VOICE = "zh-CN-XiaoxiaoNeural"
RATE = "-20%"
FORCE = "--force" in sys.argv

# (字, 主音tone3) -> 词境短语。仅用于 TTS 裸读默认音≠课内音的字（着/只），
# 其余主音读单遍（"天。"，句号让 TTS 完整收调）。alt 第二读音的词境从 CSV
# alt_readings 三元组 [拼音, tone3, 词境] 读取；无词境的 alt 跳过生成（宁缺毋错）。
PHRASE = {
    ("只", "zhi1"): "一只",
    ("着", "zhe5"): "看着",
}

# 语音指令（学前儿童不识字，题干/提示文字全部配 TTS；t_ 前缀）
# 跟读=节奏式（方案 A 2026-10-28）：无录音无按压，指令只引导"说"，不含按钮操作
PROMPTS = {
    "gate_read": "到你啦，大声读",
    "great": "读得真棒",
    "verify": "听一听，选出你听到的字",
    "q_listen": "听一听，是哪个字",
    "q_mean": "看一看词语，缺了哪个字",
    "q_sent": "看一看句子，缺了哪个字",
    "q_find": "把听到的字全都找出来",
    "q_pin": "看一看拼音，是哪个字",
    "q_rev": "听一听，出现了哪个字",
    "readcard": "今天学的字都在这些句子里，一句一句读出来吧",
    "q_write": "轮到你写啦，跟着橙色笔顺，一笔一笔写",
}


def prompt_tasks():
    return [(f"t_{k}", tx) for k, tx in PROMPTS.items()]


def py_to_tone3(py: str) -> str:
    """tiān -> tian1（去调符号标调号；ü->v，轻声->5）"""
    VOWELS = {"ā": ("a", 1), "ē": ("e", 1), "ī": ("i", 1), "ō": ("o", 1), "ū": ("u", 1), "ǖ": ("v", 1),
              "á": ("a", 2), "é": ("e", 2), "í": ("i", 2), "ó": ("o", 2), "ú": ("u", 2), "ǘ": ("v", 2),
              "ǎ": ("a", 3), "ě": ("e", 3), "ǐ": ("i", 3), "ǒ": ("o", 3), "ǔ": ("u", 3), "ǚ": ("v", 3),
              "à": ("a", 4), "è": ("e", 4), "ì": ("i", 4), "ò": ("o", 4), "ù": ("u", 4), "ǜ": ("v", 4),
              "ü": ("v", 0)}
    tone = 5
    out = []
    for c in py:
        if c in VOWELS:
            out.append(VOWELS[c][0])
            if VOWELS[c][1]:
                tone = VOWELS[c][1]
        else:
            out.append(c)
    return "".join(out) + str(tone)


def build_tasks(book="1a"):
    """字表读音 + 内容朗读 → [(fname, text)]"""
    readings, order = set(), []
    with CSV.open() as f:
        for row in csv.DictReader(f):
            if row["book"] != book:
                continue
            ch, t3, alts = row["char"], row["tone3"], row["alt_readings"]
            if (ch, t3) in readings:
                continue
            readings.add((ch, t3))
            order.append((ch, t3, False, None))
            for alt in (json.loads(alts) if alts else []):
                if (ch, alt[1]) not in readings:      # alt = [拼音, tone3, 词境]
                    readings.add((ch, alt[1]))
                    order.append((ch, alt[1], True, alt[2] if len(alt) > 2 else None))
    tasks, first_seen, skipped = [], set(), []
    for ch, t3, is_alt, alt_ctx in order:
        fname = ch if ch not in first_seen else f"{ch}_{t3}"
        first_seen.add(ch)
        if is_alt:
            if not alt_ctx:
                skipped.append(f"{fname}({t3})")      # alt 无词境 → 放弃（防默认音错读）
                continue
            tasks.append((fname, alt_ctx))
        else:
            tasks.append((fname, PHRASE.get((ch, t3), f"{ch}。")))
    if skipped:
        print("跳过无词境 alt:", skipped)
    for ch, c in json.loads(CONTENT.read_text()).items():
        if c.get("etym"):
            tasks.append((f"e_{ch}", c["etym"]))
        if c.get("words"):
            tasks.append((f"w_{ch}", "、".join(c["words"])))
            for i, w in enumerate(c["words"], 1):
                tasks.append((f"n_{ch}_{i}", w))
        for i, s in enumerate(c.get("sentences", []), 1):
            tasks.append((f"s_{ch}_{i}", s))
    return tasks


async def main():
    tasks = build_tasks() + prompt_tasks()
    OUT.mkdir(parents=True, exist_ok=True)
    # 旧版单句 s_{char}.mp3 已被 s_{char}_{i}.mp3 取代，清掉防过时内容残留
    for f in OUT.glob("s_*.mp3"):
        if not re.search(r"_\d+$", f.stem):
            f.unlink()
    sem = asyncio.Semaphore(8)

    async def bounded(fname, text):
        async with sem:
            for attempt in range(3):
                try:
                    dst = OUT / f"{fname}.mp3"
                    if FORCE or not (dst.exists() and dst.stat().st_size > 500):
                        c = edge_tts.Communicate(text, VOICE, rate=RATE)
                        await c.save(dst)
                    return fname, True
                except Exception as e:
                    if attempt == 2:
                        print("FAIL", fname, str(e)[:60])
                        return fname, False
                    await asyncio.sleep(2)

    results = await asyncio.gather(*(bounded(fn, tx) for fn, tx in tasks))
    ok = sum(1 for _, r in results if r)
    print(f"音频: {ok}/{len(tasks)}")
    bad = [fn for fn, r in results if not r]
    if bad:
        print("缺:", bad)


if __name__ == "__main__":
    asyncio.run(main())
