#!/usr/bin/env python3
"""v40 错题推关尾（方案 A）行为验证：答错→半卡→继续→下一题（非同题重测）→关末重测→关分不虚

场景：learn 日第一关 listen，第 1 题故意答错
  1. 半卡出现（wrong-note）+ 队列插入克隆（sessQueue +1，位于 gatesummary 前）
  2. 点"记住了，继续"后 curItem 为下一题（错题不再立即重现）
  3. 答对第 2 题 → 关末出现错题重测（间隔=剩余题数）
  4. 重测答对 → 关结算 1/2（首答口径，重测不计分）
用法: python3 tests/gate_wrong_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ui_test import (BASE, check, patch_playbuf, reset_test_user,  # noqa: E402
                     wait_quiz, app)
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
    page.click("button.btn.big")               # 跳过剩余（全不认识）
    page.wait_for_timeout(1200)
    page.wait_for_selector(".day-card", timeout=10000)
    page.click("button.btn.pulse", force=True)

    # 快进两个新字卡（跳步+家长跳过描红）
    for _ in range(2):
        page.wait_for_selector(".learn-card", timeout=10000)
        page.evaluate("() => window.ziapp.ncGo(4)")
        page.wait_for_timeout(400)
        page.evaluate("() => { window.ziapp.traceFallback = true; }")
        page.wait_for_timeout(200)
        page.locator(".learn-card > button.btn.big").click()
        page.wait_for_timeout(600)
    page.wait_for_function(
        "window.ziapp.curItem && window.ziapp.curItem.type === 'gatequiz'", timeout=10000)

    # ---- 第 1 题故意答错 ----
    wait_quiz(page)
    q1 = page.evaluate("() => window.ziapp.curQuiz")
    n_before = page.evaluate("() => window.ziapp.sessQueue.length")
    wrong = next(o for o in q1["options"] if o != q1["answer"])
    page.locator(".opts .opt", has_text=wrong).first.click()
    page.wait_for_function("window.ziapp.quizRevealed === true", timeout=8000)
    check("答错后半卡出现", page.locator(".wrong-note").count() == 1)
    check("半卡有继续按钮", page.locator(".wrong-note .btn").count() == 1)
    check("答错后未推进（等孩子看完半卡）",
          page.evaluate("() => window.ziapp.sessQueue.length") == n_before)

    # ---- 点继续：错题克隆插入关末（gatesummary 前），然后推进到下一题 ----
    page.click(".wrong-note .btn")
    page.wait_for_timeout(900)
    st = page.evaluate("""() => {
      const a = window.ziapp, qs = a.sessQueue;
      let pos = -1;
      for (let k = a.sessIdx + 1; k < qs.length; k++)
        if (qs[k].type === 'gatesummary' && qs[k].gate === 1) { pos = k; break; }
      const clone = pos > 0 ? qs[pos - 1] : null;
      return { len: qs.length, cloneIsWrongItem: !!(clone && clone._retest === true) };
    }""")
    check("错题克隆插入关末（间隔=剩余题数）", st["len"] == n_before + 1 and st["cloneIsWrongItem"], st)
    q2 = page.evaluate("() => window.ziapp.curQuiz")
    check("继续后是下一题（非同题立即重测）",
          q2 and q2["hanzi"] != q1["hanzi"], q2 and q2["hanzi"])
    check("下一题就绪且是第 1 关", q2 and app(page)["type"] == "gatequiz")

    # ---- 第 2 题答对 → 关末应出现错题重测 ----
    page.locator(".opts .opt", has_text=q2["answer"]).first.click()
    page.wait_for_function("window.ziapp.quizRevealed === true && window.ziapp.quizCorrect === true",
                           timeout=8000)
    page.wait_for_timeout(1200)
    page.wait_for_function(
        "window.ziapp.curItem && window.ziapp.curItem.type === 'gatequiz' "
        "&& window.ziapp.curItem._retest === true", timeout=10000)
    wait_quiz(page)
    qr = page.evaluate("() => window.ziapp.curQuiz")
    check("关末重测=错题字（间隔=关内剩余题数后重现）", qr["hanzi"] == q1["hanzi"], qr["hanzi"])

    # ---- 重测答对 → 关结算 1/2（首答口径） ----
    page.locator(".opts .opt", has_text=qr["answer"]).first.click()
    page.wait_for_function("window.ziapp.quizRevealed === true && window.ziapp.quizCorrect === true",
                           timeout=8000)
    page.wait_for_function(
        "window.ziapp.curItem && window.ziapp.curItem.type === 'gatesummary'", timeout=10000)
    txt = page.locator(".gate-card .score-xl").inner_text()
    check("关结算 1/2（重测不计分，首答口径）", txt == "1 / 2", txt)


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
