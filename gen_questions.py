#!/usr/bin/env python3
"""离线生成题库 → questions 表（待审查，verified=0）

设计（2026-10-27 题库化改造）：
- 运行时零生成：scheduler 只从 verified=1 的题取题，杜绝"程序判不了的歧义题"上线
- 校验升级：mean 干扰项代入"不得成真词"的词集 = zi_content 全库词 ∪ jieba dict.txt
  （原仅 zi_content 833 词，"＿上"代入"晚"成"晚上"这类双正确漏网由此堵死；
    jieba 仍有盲区如"课上"，语义层由逐题人工审查兜底）
- 生成期落定退化链 find→sent→mean→listen（素材不足逐级退化，不回环）
- 每字每题型先出 variant=1（首批 281×4 上限）；变体后续批次补
- payload.options 存生成时顺序；API 取题时重新 shuffle（防位置记忆，审查内容不受影响）

用法：
  python3 gen_questions.py            # 生成（幂等：清空未审查题重灌，保留审查结果）
  python3 gen_questions.py review     # 导出审查清单 data/parsed/questions_review.json
"""
import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "app"))
import scheduler  # noqa: E402  复用 CONFUSABLE / _distractors / _all_words

DB = ROOT / "app/data/zilearn.db"
DICT = ROOT / "data/words_dict.txt"
REVIEW_OUT = ROOT / "data/parsed/questions_review.json"
random.seed(20261027)  # 固定种子：可复现、审查与生成一致


def load_jieba_words():
    ws = set()
    with open(DICT, encoding="utf-8") as f:
        for line in f:
            w = line.split(" ")[0]
            if 2 <= len(w) <= 4:      # mean 挖空位代入后长度即此范围
                ws.add(w)
    return ws


def gen_for_char(db, h, content_words, content_sents, valid_words, used_stems):
    """对单字生成各题型 variant=1，返回 {quiz_type: payload}（含退化后落定类型）。

    结构约束与 scheduler.gen_quiz 一致（含 2026-10-27 二次复审后的四类硬校验：
    sent 窗口词集 / sent 题干全库唯一 / listen 选项两两异音 / mean 干扰≠题干字）；
    差异：退化在生成期落定。
    """
    ds = scheduler._distractors(db, h, h["book"])
    out = {}

    # find：词行(≤2) + 句行，复现 ≥2
    lines = [w for w in content_words if h["char"] in w][:2] + content_sents
    count = sum(l.count(h["char"]) for l in lines)
    if count >= 2:
        out["find"] = {"quiz_type": "find", "hanzi_id": h["id"], "hanzi": h["char"],
                       "tone3": h["tone3"], "lines": lines, "count": count}

    # sent：目标字恰 1 次，干扰字不入句且窗口不成词，题干全库唯一
    for s in content_sents:
        if s.count(h["char"]) != 1:
            continue
        stem = s.replace(h["char"], "＿", 1)
        if stem in used_stems:
            continue
        dr = [d["char"] for d in ds
              if d["char"] not in s
              and not scheduler._sent_window_bad(s, h["char"], d["char"], valid_words)][:3]
        if len(dr) >= 3:
            opts = [h["char"]] + dr[:3]
            random.shuffle(opts)
            out["sent"] = {"quiz_type": "sent", "hanzi_id": h["id"], "hanzi": h["char"],
                           "tone3": h["tone3"], "stem": stem,
                           "sentence": s, "options": opts, "answer": h["char"]}
            used_stems.add(stem)
            break

    # mean：目标字在词中恰 1 次，干扰≠题干字，代入不得成真词（content ∪ jieba ∪ 补充）
    for w in content_words:
        if w.count(h["char"]) != 1:
            continue
        dr, subst = [], []
        for d in ds:
            if d["char"] in w:
                continue
            rep = w.replace(h["char"], d["char"], 1)
            if rep not in valid_words:
                dr.append(d["char"])
                subst.append(rep)
            if len(dr) == 3:
                break
        if len(dr) >= 3:
            opts = [h["char"]] + dr
            random.shuffle(opts)
            out["mean"] = {"quiz_type": "mean", "hanzi_id": h["id"], "hanzi": h["char"],
                           "tone3": h["tone3"], "stem": w.replace(h["char"], "＿", 1),
                           "hint_word": w, "options": opts, "answer": h["char"]}
            out["mean_checks"] = {"substitutions": subst}   # 干扰项代入结果（审查佐证）
            break

    # listen：干扰读音必须不同，且选项间两两不同（兜底随机补足）
    seen_t = {h["tone3"]}
    dr = []
    for d in ds:
        if d["tone3"] not in seen_t:
            dr.append(d)
            seen_t.add(d["tone3"])
    if len(dr) < 3:
        for r in db.execute("SELECT char, tone3 FROM hanzi WHERE book=? AND char != ? "
                            "ORDER BY RANDOM() LIMIT 12",
                            (h["book"], h["char"])):
            if r["tone3"] not in seen_t:
                dr.append(dict(r))
                seen_t.add(r["tone3"])
            if len(dr) >= 3:
                break
    if len(dr) >= 3:
        opts = [h["char"]] + [d["char"] for d in dr[:3]]
        random.shuffle(opts)
        out["listen"] = {"quiz_type": "listen", "hanzi_id": h["id"], "hanzi": h["char"],
                         "tone3": h["tone3"], "stem": "", "options": opts, "answer": h["char"]}

    # pin 看拼音选字：题干=课内拼音，选项与 listen 同池（两两异音；池足则取后段避免与 listen 题完全同组）
    pin_dr = dr[3:6] if len(dr) >= 6 else dr[:3]
    if len(pin_dr) >= 3:
        opts = [h["char"]] + [d["char"] for d in pin_dr]
        random.shuffle(opts)
        out["pin"] = {"quiz_type": "pin", "hanzi_id": h["id"], "hanzi": h["char"],
                      "tone3": h["tone3"], "stem": h["pinyin"], "options": opts, "answer": h["char"]}

    # rev 听词选字：播整词音频选词中之字；目标字在词中恰 1 次、干扰不入词且异音
    for i, w in enumerate(content_words):
        if w.count(h["char"]) != 1:
            continue
        in_word = set(w) - {h["char"]}
        rdr = [d for d in dr if d["char"] not in in_word][:3]
        if len(rdr) >= 3:
            opts = [h["char"]] + [d["char"] for d in rdr]
            random.shuffle(opts)
            out["rev"] = {"quiz_type": "rev", "hanzi_id": h["id"], "hanzi": h["char"],
                          "tone3": h["tone3"], "stem": "", "word": w,
                          "audio": f"n_{h['char']}_{i + 1}", "options": opts, "answer": h["char"]}
            break
    return out


def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    jieba_words = load_jieba_words()
    valid_words = scheduler._valid_words(db)   # content ∪ jieba ∪ 补充（与运行时同集）
    print(f"校验词集：content ∪ jieba ∪ 补充 共 {len(valid_words)} 词（jieba 过滤后 {len(jieba_words)}）")

    # sent 题干全库唯一：以既有已审题的题干为种子（防 regen/重灌绕回已用句）
    used_stems = {json.loads(r["payload"])["stem"]
                  for r in db.execute("SELECT payload FROM questions "
                                      "WHERE quiz_type='sent' AND verified=1")}

    hanzi = db.execute("SELECT * FROM hanzi WHERE book='1a' ORDER BY seq").fetchall()
    stats = {"listen": 0, "pin": 0, "rev": 0, "mean": 0, "sent": 0, "find": 0}
    no_mean, no_sent, no_find, no_listen = [], [], [], []
    no_pin, no_rev = [], []
    rows, checks_rows = [], []
    for h in hanzi:
        c = db.execute("SELECT * FROM zi_content WHERE hanzi_id=?", (h["id"],)).fetchone()
        words = json.loads(c["words"]) if c and c["words"] else []
        sents = json.loads(c["sentences"]) if c and c["sentences"] else (
            [c["sentence"]] if c and c["sentence"] else [])
        out = gen_for_char(db, h, words, sents, valid_words, used_stems)
        for qt in ("listen", "pin", "rev", "mean", "sent", "find"):
            if qt in out:
                payload = out[qt]
                checks = out.get(f"{qt}_checks")
                rows.append((h["id"], qt, 1, json.dumps(payload, ensure_ascii=False),
                             json.dumps(checks, ensure_ascii=False) if checks else None))
                stats[qt] += 1
        for name, miss in (("listen", no_listen), ("pin", no_pin), ("rev", no_rev),
                           ("mean", no_mean), ("sent", no_sent), ("find", no_find)):
            if name not in out:
                miss.append(h["char"])

    # 幂等：未审查（verified=0）的旧生成题清除重灌；已审查（≠0）的保留不覆盖
    db.execute("DELETE FROM questions WHERE verified=0")
    db.executemany(
        "INSERT OR IGNORE INTO questions(hanzi_id, quiz_type, variant, payload, checks) "
        "VALUES(?,?,?,?,?)", rows)
    db.commit()
    total = db.execute("SELECT COUNT(*) n FROM questions").fetchone()["n"]
    print(f"生成入库：{len(rows)} 题（表内现存 {total}，含已审查保留）")
    for qt, n in stats.items():
        print(f"  {qt}: {n}/281")
    for name, miss in (("缺listen", no_listen), ("缺pin", no_pin), ("缺rev", no_rev),
                       ("缺mean", no_mean), ("缺sent", no_sent), ("缺find", no_find)):
        # 注：重灌跑批时 sent 题干唯一性种子会拦下"句已在库"的字，缺sent 数偏大属预期，
        # 其题库题由幂等保留（INSERT OR IGNORE），实际不缺。
        if miss:
            print(f"  {name} {len(miss)} 字: {''.join(miss[:40])}")
    db.close()


# ---- F 题重生成（2026-10-27 全量审查后）----
# 黑名单 = 审查中判定"代入后语义仍成立/双解"的干扰字；SKIP = 结构性问题换词/换句
SKIP_STEMS = {
    "来": ["＿了", "＿到"],   # 零线索/未到歧义，两个 F 历史题干
    # 2026-10-27 二次复审追加：同句异答冲突对（升上/升起、座位前/后、莲花/红花、右边/旁边）
    # 与零线索/感叹双解题干，regen 永久绕开
    "上": ["太阳升＿来了。"], "起": ["太阳升＿来了。"],
    "前": ["我的座位在＿面。"], "后": ["我的座位在＿面。"],
    "莲": ["＿花开了。"], "红": ["＿花开了。"],
    "右": ["学校在我家＿边。"], "旁": ["学校在我家＿边。"],
    "少": ["天上的星星多＿呀。", "多＿"],  # 多少/多小 感叹双解（sent+mean）
    "谁": ["＿的", "是＿", "＿的书包忘了？"],  # 零线索/领属开放（是谁→是他/是她同病）
    # 2026-10-27 二次复审追加：全库窗口扫描新发现的同义/双解题干
    "旦": ["元＿是新年的第一天。"],   # 元日=元旦同义
    "看": ["我＿见一只小鸟。"],       # 看见/常见皆通
    "尘": ["汽车过去，＿土飞起来。"], # 尘土/红土皆通
}
REGEN_BLACKLIST = {
    921: ["他"], 957: ["伞"], 23: ["她"], 75: ["坐"], 119: ["一"],
    123: ["几"], 127: ["几"], 397: ["昨"], 628: ["雪"], 648: ["红"],
    882: ["右"], 886: ["左"], 1124: ["未"],
    # 2026-10-27 二次复审追加（词集/窗口校验已拦，此处显式留档防 v3 绕回）
    35: ["二"], 1120: ["几"],                  # 规范/问句式双解，词集拦不住
    620: ["后"], 608: ["外", "正"], 731: ["小"], 938: ["好"], 305: ["瓜", "鱼"],
    404: ["人"], 448: ["采"], 26: ["下"], 145: ["妈"], 153: ["工"], 141: ["人"],
    209: ["学"], 695: ["红"], 317: ["里"], 942: ["也"], 652: ["金"],
    188: ["词", "棋", "桌"], 252: ["白"], 256: ["白"], 66: ["我"], 503: ["几"],
    200: ["王"], 1087: ["问"],
    730: ["小"], 663: ["红"], 851: ["常"], 977: ["姐", "妹"], 1108: ["日"],
}


def regen():
    """对 verified=-1 的题重新生成 variant=2（排除黑名单干扰/原题干），失败留待运行时退化。

    与 gen_for_char 同一套硬校验（窗口词集/题干全库唯一/两两异音/干扰≠题干字）。
    """
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    valid_words = scheduler._valid_words(db)
    # sent 题干全库唯一：以现存已审题为种子，再排除待 regen 的旧题干（-1 不入种子）
    used_stems = {json.loads(r["payload"])["stem"]
                  for r in db.execute("SELECT payload FROM questions "
                                      "WHERE quiz_type='sent' AND verified=1")}
    made, failed = [], []
    for q in db.execute("SELECT * FROM questions WHERE verified=-1").fetchall():
        h = db.execute("SELECT * FROM hanzi WHERE id=?", (q["hanzi_id"],)).fetchone()
        # 已有已审定的 v2 替代题（verified!=0）则跳过，防止重复 regen 覆盖审查结果
        has_v2 = db.execute("""SELECT 1 FROM questions WHERE hanzi_id=? AND quiz_type=?
                               AND variant=2 AND verified!=0 LIMIT 1""",
                            (q["hanzi_id"], q["quiz_type"])).fetchone()
        if has_v2:
            continue
        c = db.execute("SELECT * FROM zi_content WHERE hanzi_id=?", (h["id"],)).fetchone()
        words = json.loads(c["words"]) if c and c["words"] else []
        sents = json.loads(c["sentences"]) if c and c["sentences"] else (
            [c["sentence"]] if c and c["sentence"] else [])
        old = json.loads(q["payload"])
        bl = set(REGEN_BLACKLIST.get(q["id"], []))
        # 历史打回题干（同字反复 regen 不绕回已 F 的模式）
        bl_stems = SKIP_STEMS.get(h["char"], []) + ([old.get("stem")] if old.get("stem") else [])
        ds = [d for d in scheduler._distractors(db, h, h["book"]) if d["char"] not in bl]
        payload = None
        if q["quiz_type"] == "mean":
            for w in words:
                if w.count(h["char"]) != 1 or w == old.get("hint_word") \
                        or w.replace(h["char"], "＿", 1) in bl_stems:
                    continue
                dr = [d["char"] for d in ds if d["char"] not in w
                      and w.replace(h["char"], d["char"], 1) not in valid_words][:3]
                if len(dr) >= 3:
                    opts = [h["char"]] + dr
                    random.shuffle(opts)
                    payload = {"quiz_type": "mean", "hanzi_id": h["id"], "hanzi": h["char"],
                               "tone3": h["tone3"], "stem": w.replace(h["char"], "＿", 1),
                               "hint_word": w, "options": opts, "answer": h["char"]}
                    break
        elif q["quiz_type"] == "sent":
            for s in sents:
                if s.count(h["char"]) != 1 or s == old.get("sentence") \
                        or s.replace(h["char"], "＿", 1) in bl_stems:
                    continue
                stem = s.replace(h["char"], "＿", 1)
                if stem in used_stems:
                    continue
                dr = [d["char"] for d in ds
                      if d["char"] not in s
                      and not scheduler._sent_window_bad(s, h["char"], d["char"], valid_words)][:3]
                if len(dr) >= 3:
                    opts = [h["char"]] + dr
                    random.shuffle(opts)
                    payload = {"quiz_type": "sent", "hanzi_id": h["id"], "hanzi": h["char"],
                               "tone3": h["tone3"], "stem": stem,
                               "sentence": s, "options": opts, "answer": h["char"]}
                    used_stems.add(stem)
                    break
        if payload:
            db.execute("INSERT OR REPLACE INTO questions(hanzi_id,quiz_type,variant,payload,checks,verified,verdict) "
                       "VALUES(?,?,2,?,?,0,NULL)",
                       (h["id"], q["quiz_type"], json.dumps(payload, ensure_ascii=False), None))
            made.append((q["id"], h["char"], q["quiz_type"], payload.get("stem") or payload.get("hint_word")))
        else:
            failed.append((q["id"], h["char"], q["quiz_type"]))
    db.commit()
    print(f"重生成 {len(made)} 题（variant=2 待审）：")
    for m in made:
        print(" ", m)
    if failed:
        print(f"无替代素材 {len(failed)} 题（运行时退化链兜底）：", failed)
    db.close()


def export_review():
    """导出审查清单：紧凑题单 + 打分模板。"""
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    items = []
    for r in db.execute("""SELECT q.id, h.char, q.quiz_type, q.payload, q.checks
                           FROM questions q JOIN hanzi h ON h.id=q.hanzi_id
                           WHERE q.verified=0 ORDER BY q.id"""):
        p = json.loads(r["payload"])
        item = {"id": r["id"], "char": r["char"], "t": r["quiz_type"]}
        if r["quiz_type"] == "find":
            item["q"] = f"找「{p['hanzi']}」×{p['count']}"
            item["lines"] = p["lines"]
        else:
            item["q"] = p["stem"]
            item["opts"] = "".join(p["options"])
            item["ans"] = p["answer"]
            if r["quiz_type"] == "mean":
                item["word"] = p.get("hint_word")
                item["subst"] = json.loads(r["checks"])["substitutions"] if r["checks"] else []
        items.append(item)
    REVIEW_OUT.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"待审 {len(items)} 题已导出 {REVIEW_OUT}")
    db.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "review":
        export_review()
    elif len(sys.argv) > 1 and sys.argv[1] == "regen":
        regen()
    else:
        main()
