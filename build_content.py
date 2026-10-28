#!/usr/bin/env python3
"""内容门禁：合并 content_parts/*.json → 校验 → 写 zi_content.json

硬校验（违反即 fail，不写目标文件）：
- 覆盖：1a 全部 281 字，一字不缺
- words：1-6 个、无重复、每词 2-4 字、含目标字（AA 叠字词如"爸爸"允许，
  gen_quiz mean 出题时动态过滤 count≠1 的词，素材层只需 ≥1）
- sentences：1-3 句、无重复、去标点 4-14 字、目标字 1-3 次
  （恰 1 次是 sent 出题条件，gen_quiz 动态筛选；称谓叠字句允许 2-3 次）
- etym 非空、etym_type ∈ {picto,component,mnemonic,scene}
- emoji：空 或 不含汉字（防误填文本）

用字白名单 = 10 册教材识字表 ∪ EXEMPT（口语高频但未入 zi.txt 识字表的字，
显式列出可审计；例句目标是"孩子读得出"，不是"课本教过"）
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PARTS_DIR = ROOT / "data/parsed/content_parts"
BOOKS = ROOT / "data/parsed/zi_books.json"
OUT = ROOT / "data/parsed/zi_content.json"

PUNCT = set("，。！？、；：—…“”‘’")
TYPES = {"picto", "component", "mnemonic", "scene"}
EXEMPT = set("走过亮玩笑快给边孩己以时听海什成活变爬灯衣挂远住猫丽苹膏娃呱喔咩很睡卡")


def main():
    l1a = json.loads(BOOKS.read_text())["一年级上册"]
    valid = set()
    for chars in json.loads(BOOKS.read_text()).values():
        valid.update(chars)
    valid |= EXEMPT

    merged, errors = {}, []
    for f in sorted(PARTS_DIR.glob("p*.json")):
        for ch, c in json.loads(f.read_text()).items():
            if ch in merged:
                errors.append(f"[{ch}] 重复定义（{f.name}）")
            merged[ch] = c

    # 覆盖检查
    for ch in l1a:
        if ch not in merged:
            errors.append(f"[{ch}] 1a 字缺少内容")
    for ch in merged:
        if ch not in l1a:
            errors.append(f"[{ch}] 非 1a 字混入")

    def check_chars(text, label, ch):
        bad = [x for x in text if x not in valid and x not in PUNCT]
        if bad:
            errors.append(f"[{ch}] {label} 超纲字 {''.join(bad)}：{text}")

    for ch, c in merged.items():
        ws, ss = c.get("words", []), c.get("sentences", [])
        if not (1 <= len(ws) <= 6):
            errors.append(f"[{ch}] 词数 {len(ws)} 不在 1-6")
        if len(set(ws)) != len(ws):
            errors.append(f"[{ch}] 词重复 {ws}")
        for w in ws:
            if not (2 <= len(w) <= 4):
                errors.append(f"[{ch}] 词长异常 {w}")
            if w.count(ch) < 1:
                errors.append(f"[{ch}] 词不含目标字：{w}")
            check_chars(w, "词", ch)
        if not (1 <= len(ss) <= 3):
            errors.append(f"[{ch}] 句数 {len(ss)} 不在 1-3")
        if len(set(ss)) != len(ss):
            errors.append(f"[{ch}] 句重复")
        for s in ss:
            core = re.sub(r"[，。！？、；：—…“”‘’]", "", s)
            if not (4 <= len(core) <= 14):
                errors.append(f"[{ch}] 句长 {len(core)} 异常：{s}")
            if not (1 <= s.count(ch) <= 3):
                errors.append(f"[{ch}] 句含目标字 {s.count(ch)} 次：{s}")
            check_chars(s, "句", ch)
        if not c.get("etym"):
            errors.append(f"[{ch}] etym 为空")
        if c.get("etym_type") not in TYPES:
            errors.append(f"[{ch}] etym_type 非法 {c.get('etym_type')}")
        em = c.get("emoji", "")
        if em and re.search(r"[一-鿿]", em):
            errors.append(f"[{ch}] emoji 含汉字：{em}")

    if errors:
        print(f"✗ {len(errors)} 处违反：")
        for e in errors:
            print("  ", e)
        sys.exit(1)

    # 写目标文件：sentence 恒取 sentences[0]
    out = {ch: {**c, "sentence": c["sentences"][0]} for ch, c in
           sorted(merged.items(), key=lambda kv: l1a.index(kv[0]))}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    n_w = sum(len(c["words"]) for c in out.values())
    n_s = sum(len(c["sentences"]) for c in out.values())
    print(f"✓ {len(out)} 字 / 词 {n_w}（均 {n_w/len(out):.1f}）/ 句 {n_s}（均 {n_s/len(out):.1f}）→ {OUT}")


if __name__ == "__main__":
    main()
