#!/usr/bin/env python3
"""zilearn 字表管线（仿 wordslearn fetch_vocab.py：download 落袋 → parse 结构化）

阶段 parse: 从人教版语文课本 PDF 识字表页提取 → data/parsed/hanzi_1a_2016.csv
  - 课次分组（识字N/课文N/拼音单元/语文园地），保留教材出现顺序
  - 蓝色 span = 多音字（官方：不计入生字总数）
  - PDF 拼音字体为非标编码（Q=ā 等），投票破译后与 pypinyin 交叉验证

用法: python3 fetch_zi.py parse [pdf路径]
"""
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf
from pypinyin import pinyin, Style

ROOT = Path(__file__).parent
PDF = ROOT / "data/raw/pdf/yw1s.pdf"
OUT = ROOT / "data/parsed/hanzi_1a_2016.csv"
BLUE = 0xAE_EF  # 识字表多音字蓝


def extract_spans(pdf_path: Path):
    """识字表页 → [(text, is_blue, font_kind)] 流，font_kind: hanzi|py|other"""
    doc = pymupdf.open(pdf_path)
    spans = []
    for pno in range(119, 123):
        for b in doc[pno].get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                for s in line["spans"]:
                    t = s["text"].strip()
                    if not t:
                        continue
                    if s["font"].startswith("FZKT"):
                        spans.append((t, s["color"] == BLUE, "hanzi"))
                    elif s["font"].startswith("HanyuXi"):
                        spans.append((t, False, "py"))
                    else:
                        spans.append((t, False, "other"))
    return spans


def parse_units(spans):
    """span 流 → [(unit, char, is_blue, pdf_py_raw)] 列表"""
    rows, unit = [], None
    section = None      # '识字' | '课文' | '拼音' | '园地'
    last_cjk_unit = None  # 上一条字条目的 unit（无编号组沿用它之后的新组标记）
    yard_no = 0
    i, n = 0, len(spans)
    while i < n:
        t, blue, kind = spans[i]
        if kind == "py":
            i += 1
            continue
        flat = re.sub(r"\s+", "", t)
        m = re.fullmatch(r"（?共(\d+)个生字）?", flat)
        if m:
            assert len([r for r in rows if not r[2]]) == int(m.group(1)), \
                f"识字表黑字数 {len(rows)} != 官方标注 {m.group(1)}"
            break  # 官方注记（共300个生字）即识字表终点，其后是写字表
        if flat == "写字表":
            break  # 兜底：标题 span（文档流中晚于写字表内容）
        elif flat == "识字表":
            unit, section = None, None
        elif flat == "识字":
            section = "识字"
        elif flat == "课文":
            section = "课文"
        elif flat == "汉语拼音":
            section, unit = "拼音", "拼音单元"
        elif flat.startswith("语文园地"):
            section, unit = "园地", f"语文园地"
            yard_no += 1
        elif re.fullmatch(r"\d{1,2}", t) and int(t) <= 20:
            k = int(t)
            if section == "识字":
                unit = f"识字{k}"
            elif section == "课文":
                unit = f"课文{k}"
            elif section == "拼音":
                unit = f"拼音单元·{k}"
            # 园地无小节编号
        elif len(flat) == 1 and "一" <= flat <= "鿿":
            # 无编号裸字组（六七八九十/男女开关正反等）：沿用当前 unit 语义
            if unit is None:
                unit = "拼音单元"  # 识字5 之后、汉语拼音标签之前的组
            pdf_py = ""
            if i + 1 < n and spans[i + 1][2] == "py":
                pdf_py = spans[i + 1][0]
                i += 1
            rows.append((unit, flat, blue, pdf_py))
            last_cjk_unit = unit
        # 其余（页码/注释文字等）忽略
        i += 1
    return rows


def crack_pinyin(rows):
    """破译 PDF 拼音编码：全局投票 + 双射校验（仅作交叉验证）。

    1) 对每条 (字, 乱码)，枚举 pypinyin 全部候选读音做骨架匹配，收集每种
       (乱码字母 → 带调元音) 的投票；
    2) 每个乱码字母取多数票，要求所得映射为单射，否则该字母作废；
    3) 用映射表逐条解码，比对候选读音集合：命中→pdf读音，未命中→人工清单。
    """
    votes = Counter()
    MANUAL_TONE = {"V": "ǚ", "C": "ǜ"}  # ü 组样本少，人工确认（女nǚ 绿lǜ）
    for _, ch, _, raw in rows:
        if not raw:
            continue
        for cand in pinyin(ch, heteronym=True, style=Style.TONE)[0]:
            if len(cand) != len(raw):
                continue
            for a, b in zip(raw, cand):
                if a != b and a.isascii() and not b.isascii():
                    votes[(a, b)] += 1
    code2tone, used_tones = dict(MANUAL_TONE), set(MANUAL_TONE.values())
    for (a, b), _ in sorted(votes.items(), key=lambda kv: -kv[1]):
        if a in code2tone or b in used_tones:
            continue
        if votes[(a, b)] * 2 <= sum(v for (a2, _), v in votes.items() if a2 == a):
            continue  # 无多数票，作废
        code2tone[a], used_tones = b, b
    decoded, unresolved = {}, []
    for _, ch, _, raw in rows:
        if not raw:
            continue
        cands = pinyin(ch, heteronym=True, style=Style.TONE)[0]
        fixed = ""
        for cand in cands:
            if len(cand) != len(raw):
                continue
            if all(a == b or code2tone.get(a) == b for a, b in zip(raw, cand)):
                fixed = cand
                break
        if fixed:
            decoded[(ch, raw)] = fixed
        else:
            unresolved.append((ch, raw))
    return code2tone, decoded, unresolved




# ---------- v2: zi.txt 为权威字表（用户按 2024 课本录入，10 册） ----------

BOOK_CODE = {"一年级上册": "1a", "一年级下册": "1b", "二年级上册": "2a", "二年级下册": "2b",
             "三年级上册": "3a", "三年级下册": "3b", "四年级上册": "4a", "四年级下册": "4b",
             "五年级上册": "5a", "五年级下册": "5b"}

# 2016 版人工确认过的课内定音（同字沿用；2024 语境待用户课本核对，输出确认清单）。
# 地：2016 沿用 de 有误——2024 部编版一上"地"首现《天地人》/《雪地里的小画家》，教 dì；
#     de（轻声助词，如"轻轻地"）降为第二读音。
CARRY_TONE = {"子": "zǐ", "地": "dì", "只": "zhī", "长": "cháng", "数": "shù", "着": "zhe",
              "行": "xíng"}
# 第二读音（带词境示例，TTS 裸字只会读默认音，alt 音频必须词境消歧）
CARRY_ALT = {"地": [("de", "轻轻地")], "数": [("shǔ", "数星星")],
             "长": [("zhǎng", "长大")], "着": [("zháo", "着火")]}


def parse_zi_txt():
    """zi.txt → [(book, seq, char, pinyin, tone3, alts_json, unit)]"""
    from pypinyin import pinyin, Style
    import re as _re
    raw = (ROOT / "data/zi.txt").read_text()
    blocks = _re.split(r"【(.+?)】", raw)
    rows, poly_report = [], []
    for i in range(1, len(blocks), 2):
        title, body = blocks[i], blocks[i + 1]
        chars = []
        for line in body.splitlines():
            ls = line.strip()
            if not ls or "=" in ls or "去重" in ls or "课本" in ls:
                continue
            chars += [c for c in ls if "一" <= c <= "鿿"]
        dedup = list(dict.fromkeys(chars))
        for seq, ch in enumerate(dedup, 1):
            cands = pinyin(ch, heteronym=True, style=Style.TONE)[0]
            py = CARRY_TONE.get(ch, cands[0])
            default = cands[0]
            if len(cands) > 1 and py != default:
                poly_report.append((title, ch, py, default, "沿用2016课内音" if ch in CARRY_TONE else "?"))
            elif len(cands) > 1:
                poly_report.append((title, ch, default, "/".join(cands), "默认音，待课本核对"))
            alts = [(a, w) for a, w in CARRY_ALT.get(ch, []) if a != py]
            tone3 = py_to_tone3(py)
            alt_json = json.dumps([[a, py_to_tone3(a), w] for a, w in alts],
                                  ensure_ascii=False) if alts else ""
            unit = f"字组{(seq + 19) // 20}"
            rows.append((BOOK_CODE[title], seq, ch, py, tone3, alt_json, unit))
    return rows, poly_report


def py_to_tone3(py):
    VOWELS = {"ā": ("a", 1), "ē": ("e", 1), "ī": ("i", 1), "ō": ("o", 1), "ū": ("u", 1), "ǖ": ("v", 1),
              "á": ("a", 2), "é": ("e", 2), "í": ("i", 2), "ó": ("o", 2), "ú": ("u", 2), "ǘ": ("v", 2),
              "ǎ": ("a", 3), "ě": ("e", 3), "ǐ": ("i", 3), "ǒ": ("o", 3), "ǔ": ("u", 3), "ǚ": ("v", 3),
              "à": ("a", 4), "è": ("e", 4), "ì": ("i", 4), "ò": ("o", 4), "ù": ("u", 4), "ǜ": ("v", 4),
              "ü": ("v", 0)}
    tone, out = 5, []
    for c in py:
        if c in VOWELS:
            out.append(VOWELS[c][0])
            if VOWELS[c][1]:
                tone = VOWELS[c][1]
        else:
            out.append(c)
    return "".join(out) + str(tone)


def write_books_csv(rows):
    out = ROOT / "data/parsed/hanzi_books.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["book", "seq", "char", "pinyin", "tone3", "alt_readings", "unit"])
        w.writerows(rows)
    n_1a = sum(1 for r in rows if r[0] == "1a")
    print(f"→ {out}（共 {len(rows)} 字，一上 {n_1a}）")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "books":
        rows, poly = parse_zi_txt()
        write_books_csv(rows)
        COMMON_POLY = {"长", "着", "只", "地", "得", "数", "行", "重", "中", "好", "还",
                       "都", "发", "看", "种", "把", "没", "间", "为", "当", "相", "干",
                       "少", "分", "斗", "切", "石", "背", "兴", "觉", "落", "了", "大",
                       "空", "答", "更", "爪"}
        print("多音字确认清单（仅常用多音字，请按 mido 课本核对读音）:")
        for t in poly:
            if t[0] == "一年级上册" and t[1] in COMMON_POLY:
                print("   ", t)
        return
    pdf = Path(sys.argv[2]) if len(sys.argv) > 2 else PDF
    rows = parse_units(extract_spans(pdf))

    black = [r for r in rows if not r[2]]
    blue = [r for r in rows if r[2]]
    dup_black = [c for c, k in Counter(r[1] for r in black).items() if k > 1]
    unit_counts = Counter(r[0] for r in rows)

    code2tone, decoded, unresolved = crack_pinyin(rows)
    diffs = []
    for unit, ch, bl, raw in rows:
        fixed = decoded.get((ch, raw))
        if fixed:
            default = pinyin(ch, style=Style.TONE)[0][0]
            if fixed != default:
                diffs.append((unit, ch, fixed, default))

    print(f"识字表条目 {len(rows)} | 黑字 {len(black)}（应=300） | 蓝字 {len(blue)}")
    print(f"黑字重复: {dup_black or '无'}")
    print(f"单元分布: {dict(unit_counts)}")
    print(f"拼音映射({len(code2tone)}): " + " ".join(f"{k}→{v}" for k, v in sorted(code2tone.items())))
    print(f"解码失败(人工): {unresolved or '无'}")
    print(f"课内音≠pypinyin默认音（多音字定音清单，{len(diffs)} 个）:")
    for d in diffs:
        print("   ", d)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seq", "unit", "char", "polyphone", "pdf_pinyin", "pypinyin_default"])
        for i, (unit, ch, bl, raw) in enumerate(rows, 1):
            w.writerow([i, unit, ch, int(bl), decoded.get((ch, raw), ""),
                        pinyin(ch, style=Style.TONE)[0][0]])
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
