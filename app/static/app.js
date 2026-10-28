/* zilearn 前端 — Vue3 单文件（同 wordslearn 架构：无构建、状态机推进）
 *
 * 每日结构（learn 日）: 昨日重现(D+1批) → 新字×N(四步卡:认词句写 + 首学3题) → 今日朗读卡(例句串读) → 到期复习+抽检
 * weekly 日: 周测(scope 乱序每字1题) → 错题看半卡后同题型重测到对 → 结算星星
 * 题型: listen(听音选字) mean(组词补全) sent(句子填空) find(找一找)；测验不显示拼音（防泄漏）
 * 四步卡对标洪恩"玩认练写"+四五快读"字→词→句"递进；步指示器可点跳（家长快进）
 */
const { createApp } = Vue;
let app = null;
const ETYM = { picto: "象形字", component: "部件拆解", mnemonic: "巧记", scene: "课文场景" };
/* TTS 默认音≠课内音的字：字音文件是词境音频，按钮须明示会读到的词 */
const AUDIO_PHRASE = { "着": "看着", "只": "一只" };

async function api(method, path, body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  if (r.status === 401) { if (app) app.phase = "login"; throw new Error("未登录"); }
  const j = await r.json();
  if (!r.ok) throw new Error(j.detail || r.status);
  return j;
}

const root = createApp({
  errorHandler(err, inst, info) { console.error("[vue]", info, err && err.message); },
  data() {
    return {
      phase: "boot", loginForm: { u: "", p: "" }, loginErr: "", userName: "",
      progress: {}, obGrade: "", obTarget: 300, obDays: 50, planEst: null,
      confirmBatches: [], confirmPage: 0, confirmPicked: [],
      verifyQ: null, verifyPick: null, verifyRevealed: false,
      verifyDone: 0, verifyTotal: 0,
      today: {}, sessQueue: [], sessIdx: 0,
      ncStep: 1, readLines: [], readPlaying: false, readCur: "",
      curQuiz: null, quizPick: null, quizRevealed: false, quizCorrect: false, quizPending: null,
      weeklyDoneIds: [],
      curContent: null, weeklyScore: 0, weeklyTotal: 0, weeklyWrong: [], weeklyWrongIds: [],
      traceMsg: "", tracing: false, traceDone: false, traceFallback: false,
      findFound: [], findMisses: 0, findMiss: "",
      doneStats: { new: 0, review: 0 }, report: null,
      follow: { on: false, kind: "", idx: 0, done: [], state: "off" }, micLvl: 0,
      micDenied: false, micState: "ok", followPop: false, quizBurst: false,
      homeTip: false, homeHold: false, ncHold: -1,
      BURST_DIRS: [["-70px","-60px"],["70px","-75px"],["0px","-95px"],["-55px","55px"],["55px","65px"],["0px","-30px"]],
      gateScores: {}, gateWrong: {}, readNews: [],
    };
  },
  computed: {
    curItem() { return this.sessQueue[this.sessIdx] || null; },
    curAlt() {
      if (!this.curContent || !this.curContent.hanzi || !this.curContent.hanzi.alt_readings) return null;
      try { return JSON.parse(this.curContent.hanzi.alt_readings); } catch { return null; }
    },
    curSentences() {
      if (!this.curContent) return [];
      if (this.curContent.sentList) return this.curContent.sentList;
      return this.curContent.sentence ? [this.curContent.sentence] : [];
    },
    sessProgressPct() {
      return this.sessQueue.length ? ((this.sessIdx) / this.sessQueue.length * 100) + "%" : "0%";
    },
    todayCN() {
      const d = new Date(), wk = ["日", "一", "二", "三", "四", "五", "六"];
      return `${d.getMonth() + 1}月${d.getDate()}日 · 星期${wk[d.getDay()]}`;
    },
    estLocal() {
      const d = this.obDays || 1, t = this.obTarget || 1;
      const daily = Math.max(1, Math.ceil(t / (d - Math.floor(d / 7))));
      const est = Math.round((daily * 2.5 + daily * 4 * 0.2 + 3) * 10) / 10;
      return { daily_new: daily, est_minutes: est, over_30min: est > 30 };
    },
    estToday() {
      const n = (this.today.new || []).length, r = (this.today.review || []).length + (this.today.audit || []).length;
      return Math.round(n * 3.5 + r * 0.3 + 3.5);
    },
  },
  methods: {
    /* ---------- 通用 ---------- */
    async boot() {
      try {
        const me = await api("GET", "/api/me");
        this.progress = me.progress; this.userName = me.user.display_name || me.user.username;
        if (me.stage === "onboard") this.phase = "ob-grade";
        else if (me.stage === "plan") { this.obTarget = Math.min(300, me.progress.total_hanzi - (me.progress.skipped || 0)); this.phase = "ob-plan"; }
        else { await this.loadToday(); }
      } catch (e) { if (this.phase !== "login") this.phase = "login"; }
    },
    async doLogin() {
      this.loginErr = "";
      try {
        await api("POST", "/api/login", { username: this.loginForm.u, password: this.loginForm.p });
        await this.boot();
      } catch (e) { this.loginErr = "登录失败，再试试"; }
    },
    playChar(ch, altTone3) {
      this.stopNarration();
      if (this._charAudio) { try { this._charAudio.pause(); } catch { } }   /* 同一时刻最多一个字音（find 连点防叠音） */
      const f = altTone3 ? `${ch}_${altTone3}` : ch;
      const a = new Audio(`/static/audio/${encodeURIComponent(f)}.mp3`);
      this._charAudio = a;
      a.play().catch(() => {});
      return a;
    },
    playFile(name) {
      /* 播放单个音频文件，resolve(true)=正常播完/暂停，resolve(false)=缺文件或播放被拒
         （RCA：曾把失败静默当播完——"4 条 word 同秒"式哑播放无迹可查，现返回失败标记） */
      return new Promise((res) => {
        const a = new Audio(`/static/audio/${encodeURIComponent(name)}.mp3`);
        let failed = false;
        this._narrAudio = a;
        a.onended = () => res(!failed);
        a.onerror = () => { failed = true; res(false); };
        a.onpause = () => res(!failed);
        a.play().catch(() => { failed = true; res(false); });
      });
    },
    stopNarration() {
      /* 三通道统一停（narration 链/指令/字音）：切题、跳步、答题后遗留音频继续响的根源
         是这里只停一个通道（2026-10-28 叠音检测器抓到 4 组） */
      this._narrStop = true;
      if (this._narrAudio) { try { this._narrAudio.pause(); } catch { } this._narrAudio = null; }
      if (this._ttsAudio) { try { this._ttsAudio.pause(); } catch { } this._ttsAudio = null; }
      if (this._charAudio) { try { this._charAudio.pause(); } catch { } this._charAudio = null; }
    },
    playOne(name) { this.stopNarration(); this.playFile(name); },
    playTts(name) {                                   /* 语音指令（t_ 前缀）：单实例，新的停旧的（防连题双指令叠音） */
      try {
        if (this._ttsAudio) { try { this._ttsAudio.pause(); } catch { } }
        const a = new Audio(`/static/audio/t_${name}.mp3`);
        this._ttsAudio = a;
        a.play().catch(() => {});
      } catch { }
    },
    playSfx(name, vol = 0.6) {                        /* 反馈音效（对/错/过关）：独立短音，不打断朗读；描红逐笔用低音量 */
      try { const a = new Audio(`/static/audio/sfx_${name}.wav`); a.volume = vol; a.play().catch(() => {}); } catch { }
    },
    async stepAudio() {
      /* 四步卡每步音频+跟读（方案 A 节奏式：范读→留白跟读→✓；认步=字音→跟读→字源故事，
         保证"听到字音立刻轮到孩子"的即时模仿窗口） */
      const ch = this.curItem && this.curItem.char;
      if (!ch) return;
      this._narrStop = false;
      if (this.ncStep === 1) {
        await this.playFile(ch);
        if (this._narrStop) return;
        await this.startFollow("char");              /* 跟读（范读后"叮"提示，无啰嗦语音指令） */
        if (this._narrStop) return;
        await this.playFile(`e_${ch}`);
      } else if (this.ncStep === 2) {
        await this.playFile(`w_${ch}`);               /* 整体连听 → 逐词跟读 */
        if (this._narrStop) return;
        await this.startFollow("word");
      } else if (this.ncStep === 3) {
        await this.startFollow("sent");               /* 逐句：范读→跟读→✓ */
      }
    },
    ncGo(step) {
      /* 跳步（步指示器可点=家长快进，绕过跟读）；到写步才挂描红 */
      this.stopNarration();
      this.skipFollow();
      this.ncStep = step;
      if (step === 4) this.$nextTick(() => this.mountTracer(this.curItem.char));
      else this.stepAudio();
    },
    ncNext() {
      if (this.follow.on && this.follow.state !== "all") return;   /* 跟读未完成不放行 */
      this.ncStep >= 4 ? this.advance() : this.ncGo(this.ncStep + 1);
    },
    ncNextLabel() {
      if (this.follow.on && this.follow.state !== "all") return "🎤 先读一读 →";
      if (this.ncStep < 4) return "下一步 →";
      return this.hasNextNew() ? "这个字学会啦，下一个 →" : "我会写啦，去答题 →";
    },
    hasNextNew() {                                    /* 后面还有新字卡（写步按钮文案据此区分） */
      return this.sessQueue.slice(this.sessIdx + 1).some(q => q.type === "newcard");
    },
    async ensureMic() {
      /* 取可用麦+激活 AudioContext；引用存在≠可用：iOS 切后台/锁屏会结束 track（R2），
         每次检测前验 live，失效重取。getUserMedia（权限弹窗未决）与 resume()（无手势激活）
         都可能永久 pending，各套 3s 超时。v19 三修（RCA 五线审查抓到 v18 缺陷）：
         ① in-flight 去重——warmMic 与首个跟读并发 gUM 双双超时还互相作废；
         ② 失败分流记忆——v18 一刀切 30s 把"弹窗未决"也封死，家长点了允许还得白等；
            denied(明确拒绝)记 30s / pending(弹窗未决)记 5s / busy(设备占用)记 10s；
         ③ AbortSignal 塞 constraints 是无效成员（getUserMedia 单参数签名）——迟到流改
            引用比对收编/丢弃（宏任务时机：正常路径赋值已完成，超时路径迟到流被放掉） */
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return this._micFail("none", 60000);
      if (this._micEnsuring) return this._micEnsuring;
      if ((this._micFailUntil || 0) > Date.now()) { this.micDenied = true; return false; }
      const tmo = (p, ms) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error("mic-timeout")), ms))]);
      this._micEnsuring = (async () => {
        try {
          const live = this._micStream && this._micStream.getTracks().some(t => t.readyState === "live");
          if (!live) {
            if (this._micStream) { try { this._micStream.getTracks().forEach(t => t.stop()); } catch { } }
            const prevStream = this._micStream;         /* 竞态哨兵：await 期间被 closeMic/他处替换则丢弃迟到流 */
            const g = navigator.mediaDevices.getUserMedia({ audio: true });
            g.then(s => setTimeout(() => {                /* 迟到流（超时后才 resolve）：已被超越→停掉，别白亮指示灯 */
              if (this._micStream !== s) { try { s.getTracks().forEach(t => t.stop()); } catch { } }
            }, 0)).catch(() => { });
            const s = await tmo(g, 3000);
            if (this._micStream !== prevStream) { try { s.getTracks().forEach(t => t.stop()); } catch { } return this._micFail("busy", 1000); }
            this._micStream = s;
          }
          if (!this._micCtx) this._micCtx = new (window.AudioContext || window.webkitAudioContext)();
          if (this._micCtx.state === "suspended") await tmo(this._micCtx.resume(), 3000);
          this.micState = "ok"; this.micDenied = false;
          return true;
        } catch (e) {
          const n = e && e.name;
          if (n === "NotAllowedError" || n === "SecurityError") return this._micFail("denied", 30000);
          if (n === "NotReadableError" || n === "OverconstrainedError") return this._micFail("busy", 10000);
          return this._micFail("pending", 5000);          /* mic-timeout/其他：弹窗多半还挂着 */
        } finally { this._micEnsuring = null; }
      })();
      return this._micEnsuring;
    },
    _micFail(state, cooldownMs) {
      this.micState = state; this.micDenied = true;
      this._micFailUntil = Date.now() + cooldownMs;
      return false;
    },
    warmMic() {
      /* 跟读音量校验依赖 AudioContext.resume() 的用户激活；跟读发生时点击激活早过期，
         故在"开始学习"手势里预热。拒绝/无麦静默降级（followHeard 返回 null 不校验不夸） */
      this.ensureMic();
    },
    closeMic() {
      /* 会话结束放掉麦克风（整节课亮指示灯会让家长不安；下次 startToday 手势内重开） */
      if (this._micStream) { try { this._micStream.getTracks().forEach(t => t.stop()); } catch { } this._micStream = null; }
      if (this._micCtx) { try { this._micCtx.close(); } catch { } this._micCtx = null; }
      this._micFailUntil = 0; this.micState = "ok"; this.micDenied = false;   /* 新会话重给麦克风一次机会 */
    },
    /* ---------- 跟读（节奏式+音量校验：范读→叮→留白听你读→✓；读到才夸，无录音无回放） ---------- */
    followItems(kind) {
      if (kind === "char") return [{ key: "c", audio: this.curItem.char }];
      if (kind === "word") return (this.curContent ? this.curContent.words : [])
        .map((w, i) => ({ key: "w" + i, audio: `n_${this.curItem.char}_${i + 1}` }));
      if (kind === "sent") return this.curSentences
        .map((s, i) => ({ key: "s" + i, audio: `s_${this.curItem.char}_${i + 1}` }));
      return this.readLines.map(l => ({ key: "r" + l.char, audio: `s_${l.char}_1` }));
    },
    startFollow(kind) {
      /* 返回 Promise：跟读全流程（范读→留白→✓→夸奖，或家长跳过）结束后才 resolve——
         认步的 e_故事必须等它，否则故事压着留白与跟读反馈叠音（2026-10-28 用户抓到） */
      const items = this.followItems(kind);
      if (!items.length) return Promise.resolve();
      this.follow = { on: true, kind, idx: 0, done: [], state: "model", tries: 0, heardAny: false };
      return new Promise(res => { this._followResolve = res; this.followPlay(0); });
    },
    async followPlay(i, retry) {
      const f = this.follow;
      if (!f.on || f.state === "all") return;
      const gen = ++this._followGen;                  /* 代际 token：重听/跳过后作废旧链，防双链竞争 */
      if (!retry) f.tries = 0;
      f.idx = i; f.state = "model";
      const items = this.followItems(f.kind), it = items[i];
      if (!it) return this.followFinish();
      const micP = this.ensureMic();                  /* 范读期间并行取麦（RCA：v18 wait 后才取，首个
                                                         wait 有 1~3s 聋窗=对着空气读；且流先建立，
                                                         AEC/AGC 听过范读才预热，冷流会压童声） */
      const played = await this.playFile(it.audio);   /* 范读 */
      if (!played) console.warn("范读播放失败/被拒:", it.audio);   /* RCA：哑播放曾无迹可查 */
      if (gen !== this._followGen || !f.on) return;
      f.state = "wait";
      this.playSfx("ding");                          /* "叮"=轮到你（不用啰嗦语音指令） */
      /* 留白按内容自适应：句=范读时长+2.5s（TTS 成人语速，幼儿复述慢得多，1.5s 余量实测偏紧），词 4s，字 3.5s */
      const ms = (f.kind === "sent" || f.kind === "read")
        ? (await this.audioDur(it.audio) || 3) * 1000 + 2500
        : f.kind === "word" ? 4000 : 3500;
      if (gen !== this._followGen || !f.on) return;
      const micReady = await micP;                    /* 范读+时长查询期间取麦早该就绪；至多再等超时余量 */
      if (gen !== this._followGen || !f.on || f.state !== "wait") return;
      const heard = await this.followHeard(ms, gen, micReady);   /* 音量校验：true读到/false超时/null无麦降级 */
      this.micLvl = 0;
      if (gen !== this._followGen || !f.on || f.state !== "wait") return;
      if (heard === false && f.tries < 1) {           /* 没听到：再给一轮（重播范读） */
        f.tries++;
        return this.followPlay(i, true);
      }
      this.followDoneOne(heard);                      /* 三态透传(true读到/false超时/null无麦降级)：
                                                          家长报表 na=heard IS NULL 靠它区分，折叠成 false 会全记超时 */
    },
    async followHeard(ms, gen, micReady) {
      /* 留白窗口内监听麦克风电量（不录音不保存）：说到(true)/超时(false)/无麦或拒绝(null=节奏式降级)
         R1 动态阈值=滚动本底×2.5，绝对下限 0.05。v19 结构修复（RCA 仿真抓到自掩蔽 bug）：
         孩子说话期间 base 被语音帧自己抬升，thr=2.5×base 在语音结束前反超童声电平——本底
         0.03+童声 0.12 只净积 183ms<200ms 判超时。修法：thr 先用上一帧 base 算，voiced 帧
         (rms>thr)冻结 base；EMA 改时间基准（1-exp(-dt/825)≈60fps×0.02）——120Hz 屏（iPad
         ProMotion）base 升速不再翻倍。数值调参（地板/cap/HEARD_MS）等遥测数据再动。
         R3 真实时间累积 ~0.2s 才算读到：帧率无关，也滤掉敲一下桌子的瞬态
         达标后不立即切下一项：等静音 0.6s 让孩子把话说完（防范读抢话，读到≠读完）
         前 0.5s 排除"叮"/范读尾音的外放串扰（ding≈0.18s），本底初值取排除期最小值
         过程量（peak_rms/thr_used/…）随结果落 _fStats 供 logFollow 上报：下次报障直判"没读"还是"漏检" */
      if (!(micReady === true || await this.ensureMic())) return null;
      try {
        const src = this._micCtx.createMediaStreamSource(this._micStream);
        const an = this._micCtx.createAnalyser();
        an.fftSize = 512;
        src.connect(an);
        const buf = new Uint8Array(an.fftSize);
        const EXCL = 500, HEARD_MS = 200, QUIET_MS = 600;
        const t0 = performance.now(), hardAt = ms + EXCL + 1500;
        let base = 0.05, minExcl = 1, voicedMs = 0, trailAt = 0, lastT = t0;
        let peak = 0, thrEnd = 0;
        const done = v => {
          this._fStats = { peak_rms: +peak.toFixed(3), noise_base: +base.toFixed(3),
                           thr_used: +thrEnd.toFixed(3), voiced_ms: Math.round(voicedMs),
                           dur_ms: Math.round(performance.now() - t0) };
          return v;
        };
        try {
          return await new Promise(res => {
            const tick = () => {
              const now = performance.now(), t = now - t0;
              if (gen !== this._followGen || !this.follow.on || this.follow.state !== "wait") return res(done(false));
              an.getByteTimeDomainData(buf);
              let sum = 0;
              for (let k = 0; k < buf.length; k++) { const d = buf[k] - 128; sum += d * d; }
              const rms = Math.sqrt(sum / buf.length) / 128;
              this.micLvl = Math.min(1, rms * 8);               /* 🎤/大字随声音跳动（×8：小声读也可见） */
              const dt = now - lastT; lastT = now;
              if (t < EXCL) { minExcl = Math.min(minExcl, rms); return requestAnimationFrame(tick); }
              const thr = Math.max(0.05, base * 2.5);           /* 先用上一帧本底定阈 */
              if (rms > peak) peak = rms;
              thrEnd = thr;
              if (t < EXCL + 120) base = Math.max(0.02, minExcl);
              else if (rms <= thr) {                            /* 非语音帧才更新本底（快降慢升） */
                const a = 1 - Math.exp(-dt / 825);              /* 时间基准：帧率无关 */
                base = rms < base ? rms : base + (rms - base) * a;
              }
              if (voicedMs >= HEARD_MS) {                       /* 已读到：等孩子说完 */
                if (rms < thr) {
                  if (!trailAt) trailAt = now;
                  if (now - trailAt >= QUIET_MS) return res(done(true));
                } else trailAt = 0;
              } else {
                voicedMs = Math.max(0, voicedMs + (rms > thr ? dt : -dt * 0.5));
              }
              if (t > hardAt) return res(done(voicedMs >= HEARD_MS)); /* 持续噪音兜底：不无限等 */
              requestAnimationFrame(tick);
            };
            tick();
          });
        } finally { src.disconnect(); }
      } catch (e) { return null; }
    },
    followDoneOne(heard) {                             /* heard 三态：true读到/false超时/null无麦降级 */
      const f = this.follow, gen = this._followGen;
      if (!f.on || f.state !== "wait") return;
      this.logFollow(heard, f.tries);                         /* R5：开口流水（fire-and-forget） */
      f.heardAny = f.heardAny || !!heard;
      if (heard) {                                            /* 读到瞬间 ✓ 爆闪+推进音（反馈可感知） */
        this.followPop = true;
        clearTimeout(this._popT); this._popT = setTimeout(() => this.followPop = false, 650);
        this.playSfx("ok");
        return this.followAdvance();
      }
      if (heard === false) {                                  /* 两轮没读到：明示"没关系"再走，不播欢快
                                                                ok 音掩盖失败（RCA：v18 体感"读了没反应"来源之一） */
        f.state = "miss";
        setTimeout(() => {
          if (this._followGen === gen && f.on && f.state === "miss") this.followAdvance();
        }, 900);
        return;
      }
      this.followAdvance();                                   /* null=无麦降级：静默推进（家长在场带读） */
    },
    followAdvance() {
      const f = this.follow;
      f.done.push(f.idx);
      if (f.idx + 1 >= this.followItems(f.kind).length) this.followFinish();
      else this.followPlay(f.idx + 1);
    },
    fbHint() {                                         /* 跟读条文案分流（RCA：v18 只有 wait 两态，弹窗未决/重试/超时全靠猜） */
      const f = this.follow;
      if (f.state === "miss") return "没关系，继续～";
      if (this.micDenied) {                            /* 无麦降级：范读全程明示原因（曾只闪现 <100ms，家长无从知道为何不听） */
        if (this.micState === "pending") return "等爸爸妈妈点一下【允许】哦";
        return "麦克风没开，跟爸爸妈妈读一读吧";
      }
      if (f.state === "model") return f.tries > 0 ? "没听到哦，凑近一点，大声读～" : "先听一听…";
      if (f.state === "wait") return f.tries > 0 ? "再试一次，凑近大声读！" : "轮到你啦，大声读！";
      return "";
    },
    fbHintCls() {
      const f = this.follow;
      if (this.micDenied) return this.micState === "pending" ? "pending" : "denied";
      if (f.state === "wait") return "go";
      if (f.state === "model" && f.tries > 0) return "retry";
      if (f.state === "miss") return "miss";
      return "";
    },
    logFollow(heard, tries) {
      const f = this.follow;
      const char = f.kind === "read" ? (this.readLines[f.idx] || {}).char : this.curItem.char;
      if (!char) return;
      const s = this._fStats || {};
      api("POST", "/api/follow/log", {                        /* 过程量遥测：peak/thr/voiced/dur+UA+客户端时刻 */
        char, kind: f.kind, heard, tries,
        peak_rms: s.peak_rms ?? null, noise_base: s.noise_base ?? null,
        thr_used: s.thr_used ?? null, voiced_ms: s.voiced_ms ?? null,
        dur_ms: s.dur_ms ?? null, ts_ms: Date.now(),
        ua: navigator.userAgent.slice(0, 160),
      }).catch(() => {});
    },
    async followFinish() {
      const f = this.follow, gen = this._followGen;
      f.state = "all";
      if (f.kind === "char") await this.playFile(this.curItem.char);   /* 确认读 */
      if (f.heardAny && gen === this._followGen) await this.playFile("t_great");  /* 读到了才夸；跳过作废代际后不再播（防与后续链叠音） */
      f.on = false;                                                     /* 转自由点选模式 */
      this._resolveFollow();
    },
    _resolveFollow() {
      if (this._followResolve) { const r = this._followResolve; this._followResolve = null; r(); }
    },
    followReplay() {                                   /* 留白中点当前对象=重听范读（代际作废旧检测） */
      const f = this.follow;
      if (!f.on || f.state !== "wait") return;
      this.followPlay(f.idx);
    },
    skipFollow() {
      this._followGen = (this._followGen || 0) + 1;   /* 作废进行中的检测/播放链 */
      /* 跳过=跟读链音频（范读/确认读/夸奖）立即停。不能用 stopNarration：会置 _narrStop
         卡掉后续链（认步故事）——跳过跟读≠跳过故事（2026-10-28 叠音检测器抓到确认读×故事） */
      if (this._narrAudio) { try { this._narrAudio.pause(); } catch { } }
      this.micLvl = 0; this.followPop = false;
      this.follow = { on: false, kind: "", idx: 0, done: [], state: "off", tries: 0, heardAny: false };
      this._resolveFollow();                       /* 家长跳过也放行后续链（如认步故事） */
    },
    /* 跟读中：点当前对象重听（其余忽略）；自由模式：点对象听音 */
    followWordClick(i) {
      if (this.follow.on) { if (i === this.follow.idx) this.followReplay(); return; }
      this.playOne(`n_${this.curItem.char}_${i + 1}`);
    },
    followSentClick(i) {
      if (this.follow.on) { if (i === this.follow.idx) this.followReplay(); return; }
      this.playOne(`s_${this.curItem.char}_${i + 1}`);
    },
    followReadClick(i) {
      if (this.follow.on) { if (i === this.follow.idx) this.followReplay(); return; }
      if (this.readLines[i]) this.playOne(`s_${this.readLines[i].char}_1`);
    },
    audioDur(name) {
      return new Promise(res => {
        const a = new Audio(`/static/audio/${encodeURIComponent(name)}.mp3`);
        a.onloadedmetadata = () => res(a.duration || 0);
        a.onerror = () => res(0);
      });
    },
    skipHold(down) {                                   /* 家长长按 1.5s 跳过（防孩子误触）；返回是否达成 */
      if (down) { this._skipT = Date.now(); return false; }
      const ok = Date.now() - this._skipT >= 1500;
      if (ok) this.skipFollow();
      return ok;
    },
    wordHtml(w) { return w.split(this.curItem.char).join(`<b>${this.curItem.char}</b>`); },
    sentHtml(s) { return s.split(this.curItem.char).join(`<b>${this.curItem.char}</b>`); },
    etymName(t) { return ETYM[t] || "字的故事"; },
    listenLabel(ch) { const p = AUDIO_PHRASE[ch]; return p ? `听·${p}` : "听"; },
    inSession() {
      return ["newlearn", "review", "weekly"].includes(this.phase);
    },
    sessionLabel() {
      const t = this.curItem && this.curItem.type;
      if (t === "newcard") return "✨ 新字探索";
      if (t === "gatequiz" || t === "gatesummary") return `🚪 第${this.curItem.gate}关 · ${["听音", "拼音", "组词", "听词", "读句", "写字"][this.curItem.gate - 1]}`;
      if (this.phase === "weekly") return "🏆 周大挑战";
      if (t === "auditquiz") return "🕵️ 小侦探抽查";
      return "🎯 复习挑战";
    },
    /* ---------- 防误触（home/跳步长按化：幼儿乱点不丢进度、不跳节奏） ---------- */
    homeDown() { this._homeT = Date.now(); this.homeHold = true; },
    homeUp() {
      this.homeHold = false;
      if (Date.now() - (this._homeT || 0) >= 1200) { this.homeTip = false; this.exitSession(); }
      else this.holdTip("长按 🏠 一会儿才能回家哦");
    },
    ncDown(i) { this._ncT = Date.now(); this.ncHold = i; },
    ncUp(i) {
      this.ncHold = -1;
      if (this.ncStep === i + 1 || Date.now() - (this._ncT || 0) >= 800) this.ncGo(i + 1);
      else this.holdTip("想跳到这一步？按住一会儿");
    },
    ncLeave(i) { if (this.ncHold === i) this.ncHold = -1; },   /* 滑出走触屏常态：静默取消不提示 */
    holdTip(msg) {                                /* 长按引导统一提示（1.5s 自隐） */
      this.homeTip = msg;
      clearTimeout(this._tipT); this._tipT = setTimeout(() => this.homeTip = false, 1500);
    },
    exitSession() {                               /* skipFollow：不终止的话跟读链在退出后仍后台响（走查抓到） */
      this.stopNarration(); this.skipFollow(); this.closeMic();
      this.homeTip = false; this.sessQueue = []; this.sessIdx = 0; this.loadToday();
    },

    /* ---------- 引导流 ---------- */
    async setGrade() {
      await api("POST", "/api/profile/grade", { grade: this.obGrade });
      this.phase = "ob-plan";
    },
    async createPlan() {
      this.planEst = await api("POST", "/api/plan/create",
        { target_chars: this.obTarget, days: this.obDays });
      const r = await api("GET", "/api/confirm/batches");
      this.confirmBatches = r.batches; this.confirmPage = 0; this.confirmPicked = [];
      this.phase = "ob-confirm";
    },
    togglePick(id) {
      const i = this.confirmPicked.indexOf(id);
      i >= 0 ? this.confirmPicked.splice(i, 1) : this.confirmPicked.push(id);
    },
    async confirmSubmit() {
      /* 「跳过剩余/选完了」在任何页都直接提交：剩余批次视为不认识 */
      if (this.confirmPicked.length) await api("POST", "/api/confirm/submit", { known_ids: this.confirmPicked });
      this.verifyTotal = this.confirmPicked.length; this.verifyDone = 0;
      this.phase = "ob-verify"; this.nextVerify();
    },
    async nextVerify() {
      const r = await api("GET", "/api/confirm/verify/first");
      this.showVerifyQ(r.next_verify);
    },
    showVerifyQ(q) {
      /* 验证题到达：指令音→延迟自动播字音（对齐主流程 listen 题节奏；孩子答题后不再补播）
         局部引用必须从 this 读回（Vue3 代理）：裸 q 与 this.verifyQ 恒 !==，守卫会永久失效（startQuiz 同款） */
      this.verifyQ = q; this.verifyRevealed = false; this.verifyPick = null;
      if (!q) return this.finishVerifyStage();
      const qz = this.verifyQ;
      this.playTts("verify");
      (async () => {
        const ms = (await this.audioDur("t_verify") || 2) * 1000 + 300;
        setTimeout(() => { if (this.verifyQ === qz && !this.verifyRevealed) this.playChar(qz.hanzi); }, ms);
      })();
    },
    async verifyAnswer(o) {
      if (this.verifyRevealed) return;
      this.verifyPick = o; this.verifyRevealed = true;
      const correct = o === this.verifyQ.answer;
      this.playSfx(correct ? "ok" : "no");                 /* 对错音效：验证轮与主流程一致 */
      setTimeout(async () => {
        const r = await api("POST", "/api/confirm/verify",
          { hanzi_id: this.verifyQ.hanzi_id || this.verifyQ.id, correct });
        this.verifyDone++;
        if (r.next_verify) this.showVerifyQ(r.next_verify);
        else this.finishVerifyStage();
      }, correct ? 700 : 1300);
    },
    async finishVerifyStage() {
      const me = await api("GET", "/api/me"); this.progress = me.progress;
      this.obTarget = Math.max(10, Math.min(this.obTarget, me.progress.total_hanzi - me.progress.skipped));
      await this.loadToday();
    },

    /* ---------- 今日 ---------- */
    async loadToday() {
      this.today = await api("GET", "/api/today");
      const me = await api("GET", "/api/me"); this.progress = me.progress;
      this.phase = "today-intro";
    },
    startToday() {
      this.warmMic();                                 /* 手势上下文预热麦克风+AudioContext（跟读校验依赖） */
      if (this.today.mode === "learn") {
        if (!this.today.new.length && !this.today.review.length && !this.today.audit.length) {
          this.goParent(); return;              /* 当日已清空（重进）：直接看成绩，不空转 */
        }
        this.phase = "newlearn"; this.buildLearnQueue();
      }
      else if (this.today.mode === "weekly") { this.phase = "weekly"; this.buildWeeklyQueue(); }
      else this.phase = "done";
    },
    buildLearnQueue() {
      /* 分关结构（wordslearn v2.1 模式）：先学完 → 复习沉淀 → 分关测验（延迟回忆）→ 朗读收尾 */
      const q = [];
      const warm = this.today.review.filter(r => this.isWarm(r));
      const gen = this.today.review.filter(r => !warm.includes(r));
      warm.forEach(r => q.push({ type: "reviewquiz", phase: "review", ...r }));   // 昨日重现
      this.today.new.forEach(h => q.push({ type: "newcard", ...h }));             // 新字连播（纯呈现+跟读）
      gen.forEach(r => q.push({ type: "reviewquiz", phase: "review", ...r }));    // 复习沉淀
      this.today.audit.forEach(r => q.push({ type: "auditquiz", phase: "audit", ...r }));
      /* 分关：重进恢复——new 已空但当日学过且关未完成 → 用 learned_today 重建关 */
      const gateDone = sessionStorage.getItem("gateDone_" + new Date().toISOString().slice(0, 10)) === "1";
      const news = this.today.new.length ? this.today.new
        : (!gateDone ? (this.today.learned_today || []) : []);
      if (news.length) {
        const thirdQt = {};                        // 第三关 sent/find 按字原序交替（稳定，乱序不漂移）
        news.forEach((h, i) => { thirdQt[h.id] = i % 2 ? "sent" : "find"; });
        const gates = [
          { gate: 1, label: "听音关", qt: "listen", icon: "👂" },
          { gate: 2, label: "拼音关", qt: "pin", icon: "🔤" },
          { gate: 3, label: "组词关", qt: "mean", icon: "🧩" },
          { gate: 4, label: "听词关", qt: "rev", icon: "🎧" },
          { gate: 5, label: "读句关", qt: null, icon: "📖" },
          { gate: 6, label: "写字关", qt: "stroke", icon: "✍️" },
        ];
        gates.forEach(g => {
          const chars = [...news].sort(() => Math.random() - 0.5);   // 各关独立乱序
          if (g.qt === "stroke") {
            /* 写字关：每日 1 字随机（时长敏感，不逐字写） */
            q.push({ type: "strokegate", phase: "learn", gate: g.gate, ...chars[0] });
            q.push({ type: "gatesummary", gate: g.gate, label: g.label, icon: g.icon,
                     total: 1, isLast: true });
            return;
          }
          chars.forEach(h => q.push({ type: "gatequiz", phase: "learn",
            gate: g.gate, quiz_type: g.qt || thirdQt[h.id], ...h }));
          q.push({ type: "gatesummary", gate: g.gate, label: g.label, icon: g.icon,
                   total: chars.length });
        });
        this.gateScores = Object.fromEntries(gates.map(g => [g.gate, 0]));
        this.gateWrong = Object.fromEntries(gates.map(g => [g.gate, []]));
        q.push({ type: "readcard" });              // 朗读收尾
        this.readNews = news;                      // readcard 行=本次关池字
      } else { this.readNews = []; }
      this.sessQueue = q; this.sessIdx = 0; this.doneStats = { new: 0, review: 0 };
      this.enterItem();
    },
    isWarm(r) { return r.stage === 0; },   /* stage=0 即只学过首学，就是昨日重现批 */
    buildWeeklyQueue() {
      /* 断点续做：localStorage 记当日周测进度（首答过的字不再重出，分数/错字保留） */
      const prog = this.weeklyProg();
      const done = new Set(prog ? prog.done : []);
      const q = this.today.scope.filter(h => !done.has(h.id))
        .map(h => ({ type: "weeklyquiz", phase: "weekly", ...h }));
      for (let i = q.length - 1; i > 0; i--) {          /* 乱序（防固定顺序产生位置预期） */
        const j = Math.floor(Math.random() * (i + 1)); [q[i], q[j]] = [q[j], q[i]];
      }
      this.sessQueue = q; this.sessIdx = 0;
      this.weeklyScore = prog ? prog.score : 0;
      this.weeklyTotal = this.today.scope.length;
      this.weeklyWrong = prog ? prog.wrong : [];
      this.weeklyWrongIds = prog ? prog.wrongIds : [];
      if (!q.length) return this.finishSession();   /* 已答完未结算（最后一题答完即退出） */
      this.enterItem();
    },
    weeklyKey() { return "weekly_" + new Date().toISOString().slice(0, 10); },
    weeklyProg() { try { return JSON.parse(localStorage.getItem(this.weeklyKey())) || null; } catch { return null; } },
    weeklyLeft() { const p = this.weeklyProg(); return p && this.today.scope ? this.today.scope.length - p.done.length : null; },
    weeklySave() {
      localStorage.setItem(this.weeklyKey(), JSON.stringify(
        { done: this.weeklyDoneIds, score: this.weeklyScore, wrong: this.weeklyWrong, wrongIds: this.weeklyWrongIds }));
    },

    /* ---------- 会话推进 ---------- */
    enterItem() {
      const it = this.curItem;
      this.curQuiz = null; this.quizRevealed = false; this.quizCorrect = false; this.curContent = null;
      this.tracing = false; this.traceMsg = "✏️ 点米字格，跟着橙色笔顺写一写";
      this.findFound = []; this.findMisses = 0; this.findMiss = "";
      this.ncStep = 1; this.readPlaying = false; this.readCur = "";
      this.stopNarration();
      this.skipFollow();
      if (it && it.type === "newcard") this.learnDone = false;
      if (!it) return this.finishSession();
      if (it.type === "newcard") {
        const item = it;
        this.mountContent(it.id).then(() => {
          if (this.curItem !== item) return;          /* 已切卡则不播 */
          this.stepAudio();
        });
      } else if (it.type === "readcard") {
        const item = it;
        this.loadReadLines().then(async () => {
          if (this.curItem !== item) return;
          this._narrStop = false;
          await this.playFile("t_readcard");
          if (!this._narrStop) await this.startFollow("read");   /* 逐句跟读 */
        });
      } else if (it.type === "strokegate") {
        this.mountStrokeQuiz(it);
      } else if (it.type.endsWith("quiz")) {
        this.startQuiz(it);
      }
    },
    async mountContent(hid) {
      const r = await api("GET", `/api/hanzi/${hid}/full`);
      let words = [], sentList = [];
      try { words = r.content && r.content.words ? JSON.parse(r.content.words) : []; }
      catch { words = []; }
      try { sentList = r.content && r.content.sentences ? JSON.parse(r.content.sentences) : []; }
      catch { sentList = []; }
      if (!sentList.length && r.content && r.content.sentence) sentList = [r.content.sentence];
      this.curContent = r.content ? { ...r.content, words, sentList, hanzi: r.hanzi }
                                  : { hanzi: r.hanzi, words, sentList };
    },
    mountTracer(char) {
      const el = document.getElementById("hw-" + this.curItem.id);
      if (!el || !window.HanziWriter) return;
      try {
        const w = HanziWriter.create(el, char, {
          width: 210, height: 210, padding: 5, showOutline: true,
          strokeAnimationSpeed: 1.2, strokeColor: "#ff9800", outlineColor: "#ffe0b2",
          charDataLoader: (c, done) =>
            fetch(`/static/strokes/${encodeURIComponent(c)}.json`)
              .then(r => r.ok ? r.json() : null).then(done),
        });
        w.loopCharacterAnimation();
        el.onclick = () => this.startTrace(el, w, char);
      } catch (e) { /* 描红失败不阻塞学习流 */ }
    },
    startTrace(el, w, char) {
      if (this.tracing) return;
      this.tracing = true;
      el.classList.add("tracing");
      this.traceMsg = "跟着橙色提示，写下第 1 笔吧 ✍️";
      w.quiz({
        leniency: 1.4, showHintAfterMisses: 1,
        onCorrectStroke: (d) => { this.traceMsg = `真棒！第 ${d.strokeNum + 1} 笔写对了，继续～`; this.playSfx("ok", 0.3); },
        onMistake: () => { this.traceMsg = "没关系，再试一次～"; },
        onComplete: () => {
          el.classList.remove("tracing"); el.classList.add("done");
          this.tracing = false;
          this.traceMsg = "写完啦 🎉 再点一下还能再写一遍";
          this.playChar(char);
        },
      });
    },
    /* ---------- 写字关（每日 1 字，写完才过关；HanziWriter quiz 复用写步参数） ---------- */
    mountStrokeQuiz(it) {
      this.traceDone = false; this.traceFallback = false;
      this.traceMsg = "✏️ 点米字格，跟着橙色笔顺写一写";
      this.playTts("q_write");
      this.$nextTick(() => {
        const el = document.getElementById("hw-gate");
        if (!el || !window.HanziWriter) { this.traceFallback = true; return; }
        try {
          const w = HanziWriter.create(el, it.char, {
            width: 210, height: 210, padding: 5, showOutline: true,
            strokeAnimationSpeed: 1.2, strokeColor: "#ff9800", outlineColor: "#ffe0b2",
            charDataLoader: (c, done) =>
              fetch(`/static/strokes/${encodeURIComponent(c)}.json`)
                .then(r => { if (!r.ok) throw 0; return r.json(); })
                .then(done)
                .catch(() => { this.traceFallback = true; done(null); }),
          });
          w.loopCharacterAnimation();
          el.onclick = () => {
            if (this.traceDone || this.tracing) return;
            this.tracing = true; el.classList.add("tracing");
            this.traceMsg = `跟着橙色提示，把「${it.char}」一笔一笔写完吧 ✍️`;
            w.quiz({
              leniency: 1.4, showHintAfterMisses: 1,
              onCorrectStroke: d => { this.traceMsg = `真棒！第 ${d.strokeNum + 1} 笔写对了，继续～`; this.playSfx("ok", 0.3); },
              onMistake: () => { this.traceMsg = "没关系，跟着虚线再试一次～"; },
              onComplete: () => {
                el.classList.remove("tracing"); el.classList.add("done");
                this.tracing = false; this.traceDone = true;
                this.traceMsg = "写完啦 🎉";
                this.gateScores[it.gate] = 1;
                this.playChar(it.char); this.playSfx("ok");
                api("POST", "/api/submit", { hanzi_id: it.id, phase: "learn",
                  quiz_type: "stroke", correct: true });
              },
            });
          };
        } catch { this.traceFallback = true; }
      });
    },
    /* ---------- 今日朗读卡（今日新字例句串读，目标字高亮） ---------- */
    async loadReadLines() {
      const news = (this.readNews && this.readNews.length) ? this.readNews : (this.today.new || []);
      const lines = await Promise.all(news.map(async h => {
        try {
          const r = await api("GET", `/api/hanzi/${h.id}/full`);
          let sents = [];
          try { sents = r.content && r.content.sentences ? JSON.parse(r.content.sentences) : []; }
          catch { sents = []; }
          return { char: h.char, sent: sents[0] || (r.content && r.content.sentence) || "" };
        } catch { return { char: h.char, sent: "" }; }
      }));
      this.readLines = lines.filter(l => l.sent);
    },
    readHtml(sent) {
      let out = sent;
      for (const h of (this.today.new || [])) out = out.split(h.char).join(`<b>${h.char}</b>`);
      return out;
    },
    readPlayAll() {
      if (this.readPlaying) { this.stopNarration(); this.readPlaying = false; this.readCur = ""; return; }
      this.readPlaying = true;
      this._narrStop = false;
      (async () => {
        for (const l of this.readLines) {
          if (this._narrStop) break;
          this.readCur = l.char;
          await this.playFile(`s_${l.char}_1`);
        }
        this.readPlaying = false; this.readCur = "";
        if (!this._narrStop) this.playSfx("ding");       /* 连读完"叮"提示（不用啰嗦语音指令） */
      })();
    },
    async startQuiz(it) {
      let qt = it.quiz_type || this.randType(it.id);
      this.curQuiz = await api("GET", `/api/quiz?hanzi_id=${it.id}&quiz_type=${qt}`);
      if (!this.curQuiz) {                          /* 素材缺失兜底：防永久卡"出题中…" */
        this.traceMsg = "这题先跳过啦";
        setTimeout(() => this.advance(), 1200);
        return;
      }
      this.quizPending = it;
      const aqt = this.curQuiz.quiz_type;   /* 实际题型（可能与请求不同：题库退化） */
      const PROMPT = { listen: "q_listen", mean: "q_mean", sent: "q_sent",
                       find: "q_find", pin: "q_pin", rev: "q_rev" };
      if (PROMPT[aqt]) this.playTts(PROMPT[aqt]);
      /* 自动播音用局部引用：孩子快速答完 advance 后 curQuiz 已置空，读它即崩；
         且切题后不再播（curQuiz 守卫），防旧题字音叠到新题；
         延迟=指令实际时长+300ms（固定延迟曾与指令尾叠音） */
      const qz = this.curQuiz;
      const promptMs = PROMPT[aqt] ? (await this.audioDur(`t_${PROMPT[aqt]}`) || 2) * 1000 + 300 : 400;
      if (aqt === "listen") setTimeout(() => { if (this.curQuiz === qz) this.playChar(qz.hanzi); }, promptMs);
      else if (aqt === "find") setTimeout(() => { if (this.curQuiz === qz) this.playChar(qz.hanzi); }, promptMs);
      else if (aqt === "rev") setTimeout(() => { if (this.curQuiz === qz) this.playFile(qz.audio); }, promptMs);
    },
    /* ---------- 找一找（点出句/词中所有目标字） ---------- */
    findTap(li, ti, ch) {
      const q = this.curQuiz;
      if (!q || q.quiz_type !== "find" || this.quizRevealed) return;
      const key = `${li}-${ti}`;
      if (this.findFound.includes(key)) return;
      if (ch === q.hanzi) {
        this.findFound.push(key);
        this.playChar(q.hanzi);
        if (this.findFound.length >= q.count) this.findDone(false);
      } else {
        this.playSfx("no");
        this.findMiss = key;
        setTimeout(() => { this.findMiss = ""; }, 400);
        this.findMisses++;
        if (this.findMisses >= 2) this.findDone(true);
      }
    },
    findDone(failed) {
      if (!failed) { this.playSfx("ok"); this.burst(); }
      else setTimeout(() => {              /* 答错自动播正确字音（同 pickOpt） */
        if (this.curQuiz && this.quizRevealed && !this.quizCorrect) this.playChar(this.curQuiz.hanzi);
      }, 600);
      this.quizRevealed = true;
      this.quizCorrect = !failed;
      this.submitQuiz(this.quizCorrect);   /* advance 统一由 submitQuiz 负责（双定时器曾跳卡） */
    },
    randType(hid) {
      return ["listen", "mean", "sent", "find"][Math.floor(Math.random() * 4)];
    },
    optClass(o) {
      if (!this.quizRevealed) return "";
      return o === this.curQuiz.answer ? "good" :
        (o === this.quizPick ? "bad" : "");
    },
    async pickOpt(o) {
      if (this.quizRevealed) return;
      this.stopNarration();                       /* 已作答即停指令/遗留音（防 s_ 回放与指令尾叠） */
      this.quizPick = o; this.quizRevealed = true;
      const qz = this.curQuiz;
      this.quizCorrect = o === qz.answer;
      this.playSfx(this.quizCorrect ? "ok" : "no");
      if (this.quizCorrect) {
        this.burst();                             /* 答对星星迸发：夸奖要看得见 */
        if (qz.quiz_type === "sent")
          setTimeout(() => this.playFile(`s_${qz.hanzi}_1`), 450);   /* 音效后再听整句 */
      } else {
        setTimeout(() => {                        /* 答错 600ms 后自动播正确字音：错题学习闭环 */
          if (this.curQuiz === qz && this.quizRevealed) this.playChar(qz.hanzi);
        }, 600);
      }
      await this.submitQuiz(this.quizCorrect);
    },
    burst() {                                      /* 星星迸发 0.8s（答题正确路径统一触发） */
      this.quizBurst = true;
      clearTimeout(this._burstT); this._burstT = setTimeout(() => this.quizBurst = false, 800);
    },
    async submitQuiz(correct) {
      const it = this.quizPending;
      if (it.type === "gatequiz") {
        /* 关题：调度已在卡片完成时注册（learn 幂等），此处只记流水+关分；答错半卡重测到对 */
        const first = !it._answered; it._answered = true;
        await api("POST", "/api/submit", { hanzi_id: it.id, phase: "learn",
          quiz_type: this.curQuiz.quiz_type, correct });
        if (first) {
          if (correct) this.gateScores[it.gate]++;
          else this.gateWrong[it.gate].push(this.curQuiz.hanzi);
        }
        if (correct) setTimeout(() => this.advance(), 900);
        else this.mountContent(it.id);
        return;
      }
      /* 重测（quizContinue 后再答）不提交不计数：错题回炉轨迹以首答为准，周测分不虚高 */
      const first = !it._answered; it._answered = true;
      /* 复习/周测/抽检答错：拉内容给"先看半卡再重测" */
      if (!correct) this.mountContent(it.id);
      if (first) {
        await api("POST", "/api/submit", { hanzi_id: it.id, phase: it.phase,
          quiz_type: this.curQuiz.quiz_type, correct });
        if (!correct && this.phase === "weekly") {
          this.weeklyWrong.push(this.curQuiz.hanzi);
          this.weeklyWrongIds.push(it.id);
        }
        if (this.phase === "weekly") { this.weeklyDoneIds.push(it.id); this.weeklySave(); }
      }
      if (correct) {
        if (first && this.phase === "weekly") this.weeklyScore++;
        setTimeout(() => this.advance(), 900);
      }
    },
    async quizContinue() {
      const it = this.quizPending;
      if (it.type === "gatequiz" || it.type === "reviewquiz" || it.type === "weeklyquiz" || it.type === "auditquiz") {
        /* 复习/周测错题：同题型重测到对（不重复提交，回炉轨迹完整） */
        this.quizRevealed = false; this.quizPick = null;
        this.findFound = []; this.findMisses = 0; this.findMiss = "";
        this.curQuiz = await api("GET", `/api/quiz?hanzi_id=${it.id}&quiz_type=${this.curQuiz.quiz_type}`);
        if (this.curQuiz.quiz_type === "listen") setTimeout(() => this.playChar(this.curQuiz.hanzi), 400);
        return;
      }
      this.advance();
    },
    advance() {
      const done = this.curItem;
      /* 新字卡四步完成 → 提交首学（注册 D+1 复习；分关题只记流水不再动调度） */
      if (done && done.type === "newcard" && !this.learnDone) {
        this.learnDone = true;
        api("POST", "/api/submit", { hanzi_id: done.id, phase: "learn",
          quiz_type: "card", correct: true });
      }
      /* 全部关过完 → 标记当日关完成（重进不重建关队列） */
      if (done && done.type === "gatesummary" && done.isLast)
        sessionStorage.setItem("gateDone_" + new Date().toISOString().slice(0, 10), "1");
      this.stopNarration();
      this.sessIdx++;
      this.enterItem();
    },
    async finishSession() {
      this.closeMic();
      if (this.phase === "weekly") {
        await api("POST", "/api/weekly/finish", { week_no: this.today.week_no,
          score: this.weeklyScore, total: this.weeklyTotal, wrong_ids: this.weeklyWrongIds });
        localStorage.removeItem(this.weeklyKey());   /* 已结算，清断点 */
        this.phase = "weekly-done";
      } else {
        this.doneStats.new = this.today.new.length || (this.today.learned_today || []).length;   /* 重进恢复场景 new 为空 */
        this.doneStats.review = this.today.review_done != null ? this.today.review_done
          : this.today.review.length + this.today.audit.length;   /* 重进后 review 只剩未答项，取当日已答真源（scheduler review_done） */
        this.phase = "done";
      }
      this.playSfx("win");
      const me = await api("GET", "/api/me"); this.progress = me.progress;
    },
    async goParent(from) {
      this.report = await api("GET", "/api/report/weekly");
      const me = await api("GET", "/api/me"); this.progress = me.progress;
      this.phase = "parent";
    },
  },
  async mounted() { this.boot(); },
});
app = root.mount("#app");
window.ziapp = app;
