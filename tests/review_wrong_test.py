#!/usr/bin/env python3
"""v67 复习错题隔段重现（方案 A 推广）行为验证：答错→半卡→继续→下一题（非同题重测）
→复习环节末尾重现（插在下一个非 reviewquiz 项之前，间隔=剩余复习题数）→重测再错不再推、放行

场景：day1 学 2 字 → skipDay 到 day2（两字 D+1 昨日重现，stage0 warm 排复习段最前）
  1. 第 1 复习题答错：半卡出现、未推进
  2. 点继续：推进到第 2 复习题（非同题），克隆插在复习段末（第一个非 reviewquiz 项之前）
  3. 第 2 题答对 → 重测出现（_retest，同字）
  4. 重测故意再答错 → 不再克隆、放行进下一环节（D+1 回炉兜底）
用法: python3 tests/review_wrong_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ui_test import (BASE, check, patch_playbuf, reset_test_user,  # noqa: E402
                     wait_quiz, answer_quiz, answer_quiz_wrong)
from playwright.sync_api import sync_playwright  # noqa: E402


def run(page):
    reset_test_user()
    page.goto(BASE, wait_until="networkidle")
    patch_playbuf(page)
    page.fill("input[autocomplete=username]", "test")
    page.fill("input[type=password]", "test123")
    page.click("button.btn.big")
    page.wait_for_selector(".wizard", timeout=10000)
    page.evaluate("() => { window.ziapp.followHeard = (ms, gen) => new Promise(r => setTimeout(() => r(true), 300)); }")
    page.click(".grade:first-child")
    page.click("button.btn.big")
    page.wait_for_timeout(600)
    page.fill("input[type=number]", "52")
    page.locator(".chip", has_text="30 天").click()
    page.click("button.btn.big")
    page.wait_for_selector(".char-grid", timeout=10000)
    page.locator("button.btn.ghost", has_text="跳过剩余").click()   # 非末页为 ghost（2026-11-14 拍板）
    page.wait_for_timeout(1200)
    page.wait_for_selector(".day-card", timeout=10000)
    page.click("button.btn.pulse", force=True)

    # day1：快进两个新字卡（learn 提交 → 明日 D+1 昨日重现）
    for _ in range(2):
        page.wait_for_selector(".learn-card", timeout=10000)
        page.evaluate("() => window.ziapp.ncGo(4)")
        page.wait_for_timeout(400)
        page.evaluate("() => { window.ziapp.traceFallback = true; }")
        page.wait_for_timeout(200)
        page.locator(".learn-card > button.btn.big").click()
        page.wait_for_timeout(600)
    # skipDay → day2：队列以 2 条 warm 复习题开头
    page.evaluate("async () => { await window.ziapp.skipDay(); }")
    page.wait_for_selector(".day-card", timeout=10000)
    page.click("button.btn.pulse", force=True)
    page.wait_for_function(
        "window.ziapp.curItem && window.ziapp.curItem.type === 'reviewquiz'", timeout=10000)

    # ---- 第 1 复习题故意答错 ----
    wait_quiz(page)
    q1 = page.evaluate("() => window.ziapp.curQuiz")
    n_before = page.evaluate("() => window.ziapp.sessQueue.length")
    answer_quiz_wrong(page)
    page.wait_for_function("window.ziapp.quizRevealed === true", timeout=8000)
    check("答错后半卡出现", page.locator(".wrong-note").count() == 1)
    check("答错后未推进（等孩子看完半卡）",
          page.evaluate("() => window.ziapp.sessQueue.length") == n_before)

    # ---- 点继续：推进到第 2 复习题，克隆插在复习段末 ----
    page.click(".wrong-note .btn")
    page.wait_for_timeout(900)
    st = page.evaluate("""() => {
      const a = window.ziapp, qs = a.sessQueue;
      let segEnd = -1;                       // 复习段边界=第一个非 reviewquiz 项
      for (let k = a.sessIdx + 1; k < qs.length; k++)
        if (qs[k].type !== 'reviewquiz') { segEnd = k; break; }
      let cloneIdx = -1;
      for (let k = 0; k < qs.length; k++)
        if (qs[k]._retest === true) { cloneIdx = k; break; }
      return { segEnd, cloneIdx, curId: a.curItem && a.curItem.id,
               cloneId: cloneIdx >= 0 ? qs[cloneIdx].id : null };
    }""")
    check("继续后推进非同题（非立即重测）", st["curId"] != q1["hanzi_id"], st)
    check("错题克隆插在复习段末（下一环节项之前）",
          st["cloneIdx"] == st["segEnd"] - 1 and st["cloneId"] == q1["hanzi_id"], st)

    # ---- 第 2 复习题答对 → 复习段末出现错题重测 ----
    answer_quiz(page)
    page.wait_for_timeout(1200)
    page.wait_for_function(
        "window.ziapp.curItem && window.ziapp.curItem._retest === true", timeout=10000)
    wait_quiz(page)
    qr = page.evaluate("() => window.ziapp.curQuiz")
    check("段末重测=错题字（间隔=剩余复习题数后重现）", qr["hanzi"] == q1["hanzi"], qr["hanzi"])

    # ---- 重测故意再答错 → 不再克隆、放行进下一环节 ----
    n_mid = page.evaluate("() => window.ziapp.sessQueue.length")
    answer_quiz_wrong(page)
    page.wait_for_function("window.ziapp.quizRevealed === true", timeout=8000)
    page.click(".wrong-note .btn")
    page.wait_for_timeout(900)
    st2 = page.evaluate("""() => {
      const a = window.ziapp;
      return { n: a.sessQueue.length,
               retestN: a.sessQueue.filter(x => x._retest === true).length,
               curType: a.curItem && a.curItem.type };
    }""")
    check("重测再错不再克隆（上限 1 次）",
          st2["n"] == n_mid and st2["retestN"] == 1, st2)
    check("重测再错放行进下一环节（D+1 回炉兜底）", st2["curType"] == "newcard", st2)


if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/root/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--autoplay-policy=no-user-gesture-required",
                  "--use-fake-device-for-media-stream",
                  "--use-fake-ui-for-media-stream",
                  "--use-file-for-fake-audio-cycles=/root/zilearn/tests/fake_mic.wav"])
        page = browser.new_page(viewport={"width": 820, "height": 1100})
        try:
            run(page)
        finally:
            browser.close()
    import sys as _s
    from ui_test import PASS_N, FAIL_N
    print(f"\n{PASS_N} pass / {FAIL_N} fail")
    _s.exit(1 if FAIL_N else 0)
