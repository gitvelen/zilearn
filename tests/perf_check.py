#!/usr/bin/env python3
"""v40 流畅度自测：advance→题就绪延迟 + 音频预载完成度（本地直连，验证预取链无串行等待）

测量（注入 advance/startQuiz 包装）：
  - quiz_ready_ms：advance 调用 → 下一道 quiz 的 curQuiz 就绪（预取命中应 <50ms 本地）
  - audio_ready_ms：advance 调用 → 该题题干音频 decode 完成可播（_loadBuf 缓存命中即 <20ms）
统计 P50/P95/max，≥300ms 视为可感知卡顿（公网 RTT 下预取命中同样快，未命中≈RTT）
用法: python3 tests/perf_check.py
"""
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ui_test import BASE, patch_playbuf, reset_test_user  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

HOOK = """(() => {
  if (window.__perfHooked) return; window.__perfHooked = true;
  window.__perfLog = [];
  const a = window.ziapp;
  const origAdvance = a.advance.bind(a);
  a.advance = function () {
    const t0 = performance.now();
    const nxt = a.sessQueue[a.sessIdx + 1];
    const r = origAdvance();
    if (nxt && nxt.type && nxt.type.endsWith('quiz')) {
      const check = () => {
        const it = a.curItem;
        if (!it || a.sessIdx === undefined) return;
        if (it.type && it.type.endsWith('quiz') && a.curQuiz) {
          const ms = Math.round(performance.now() - t0);
          let audioReady = null;
          if (a._playCtx) {
            const t = a.curQuiz.quiz_type;
            let f = null;
            if (t === 'listen' || t === 'find') f = a.curQuiz.hanzi;
            else if (t === 'rev' || t === 'lsent' || t === 'defi') f = a.curQuiz.audio;
            else if (t === 'sent') f = 's_' + a.curQuiz.hanzi + '_1';
            if (f) {
              const url = '/static/audio/' + encodeURIComponent(f) + '.mp3';
              const buf = a._bufC && a._bufC.get(url);
              if (buf) {
                const d = performance.now();
                a._loadBuf(a._playCtx, url).then(() => {
                  audioReady = Math.round(performance.now() - d);
                });
              }
            }
          }
          window.__perfLog.push({ ms, audioReady, qt: a.curQuiz.quiz_type });
        } else if (it && it.type && it.type.endsWith('quiz')) {
          setTimeout(check, 15);
        }
      };
      setTimeout(check, 0);
    }
    return r;
  };
})()"""


def run(page):
    reset_test_user()
    page.goto(BASE, wait_until="networkidle")
    patch_playbuf(page)
    page.fill("input[autocomplete=username]", "test")
    page.fill("input[type=password]", "test123")
    page.click("button.btn.big")
    page.wait_for_selector(".wizard", timeout=10000)
    page.evaluate("() => { window.ziapp.followHeard = (ms, gen) => new Promise(r => setTimeout(() => r(true), 200)); }")
    page.evaluate(HOOK)
    page.click(".grade:first-child")
    page.click("button.btn.big")
    page.wait_for_timeout(600)
    page.fill("input[type=number]", "52")
    page.locator(".chip", has_text="30 天").click()
    page.click("button.btn.big")
    page.wait_for_selector(".char-grid", timeout=10000)
    page.click("button.btn.big")
    page.wait_for_timeout(1200)
    page.wait_for_selector(".day-card", timeout=10000)
    page.click("button.btn.pulse", force=True)

    # 快进两个新字卡
    for _ in range(2):
        page.wait_for_selector(".learn-card", timeout=10000)
        page.evaluate("() => window.ziapp.ncGo(4)")
        page.wait_for_timeout(350)
        page.evaluate("() => { window.ziapp.traceFallback = true; }")
        page.wait_for_timeout(150)
        page.locator(".learn-card > button.btn.big").click()
        page.wait_for_timeout(500)

    # 走完五关（写字关已删），全部答对；错一题验重测路径
    for _ in range(60):
        st = page.evaluate("""() => { const a = window.ziapp;
            if (!['newlearn', 'review', 'weekly'].includes(a.phase)) return { done: true };
            if (!a.sessQueue.length) return { done: true };
            return { t: a.curItem && a.curItem.type, hw: !!a.curQuiz }; }""")
        if st.get("done") or st.get("t") == "readcard":
            break
        t = st.get("t")
        if t == "gatequiz":
            page.wait_for_function("window.ziapp.curQuiz", timeout=10000, polling=50)
            page.evaluate("""() => { const a = window.ziapp, q = a.curQuiz;
                if (!q) return;
                if (q.options) a.pickOpt(q.answer);
                else if (q.quiz_type === 'find') {
                  for (let li = 0; li < q.lines.length; li++)
                    for (let ti = 0; ti < q.lines[li].length; ti++)
                      if (q.lines[li][ti] === q.hanzi) a.findTap(li, ti, q.hanzi);
                } }""")
            page.wait_for_timeout(1250)       # 900ms 停留 + 余量
        elif t == "gatesummary":
            page.locator(".gate-card .btn.big").click()
            page.wait_for_timeout(400)
        else:
            page.evaluate("() => window.ziapp.advance()")
            page.wait_for_timeout(300)

    log = page.evaluate("() => window.__perfLog")
    ms_all = sorted(x["ms"] for x in log)

    def pct(v, p):
        return v[min(len(v) - 1, int(len(v) * p))]

    print(f"\n样本 {len(ms_all)} 次 advance→题就绪（本地直连，预取命中=无网络串行）：")
    print(f"  P50 {statistics.median(ms_all):.0f}ms | P90 {pct(ms_all, .9)}ms | "
          f"P95 {pct(ms_all, .95)}ms | max {max(ms_all)}ms")
    slow = [x for x in log if x["ms"] >= 300]
    print(f"  ≥300ms 可感知卡顿 {len(slow)} 次：{slow[:5]}")
    n_audio = sum(1 for x in log if x.get("audioReady") is not None)
    if n_audio:
        av = sorted(x["audioReady"] for x in log if x.get("audioReady") is not None)
        print(f"  题干音频预载命中（advance 时已 decode）：{n_audio}/{len(log)} "
              f"（未命中者现场加载：max {max(av) if av else 0}ms）")
    ok = len(ms_all) >= 8 and len(slow) == 0 and statistics.median(ms_all) < 50
    print("  结论：", "✓ 流畅（预取链无串行等待）" if ok else "✗ 存在卡顿，需排查")
    return ok


if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/root/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--autoplay-policy=no-user-gesture-required",
                  "--use-fake-device-for-media-stream",
                  "--use-fake-ui-for-media-stream"])
        page = browser.new_page(viewport={"width": 820, "height": 1100})
        try:
            ok = run(page)
        finally:
            browser.close()
    sys.exit(0 if ok else 1)
