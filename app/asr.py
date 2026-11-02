"""词步跟读 ASR 校验：录音 → 转写 → 拼音容错匹配。

设计约束（2026-11-01 用户拍板）：
- 只做内容匹配不做发音评分：识别文本与目标词做**声调容错**的拼音比对，
  防的是"啊——"乱喊骗过音量检测，不纠发音（幼儿声调习得晚，读近似音也算对）。
- 判定对孩子无感知：结果只影响节奏（过=推进，不过=范读重播），前端无评判反馈。
- ASR 是增强不是依赖：key 未配置/网络失败/超时/格式不支持 → 一律降级放行
  （degraded），孩子端永不因它卡流程。

provider 用 OpenAI 兼容 transcriptions 协议（multipart），默认智谱 glm-asr-2512
（2026-11-02 实测：coding plan key 可直接调用，1.6s 童声 mp3 识别准确，0.06 元/分钟）。
智谱只收 .wav/.mp3——前端 Safari=audio/mp4、Chrome=audio/webm 实测均被 1214 拒，
故非 wav/mp3 一律先经 ffmpeg 转 16k 单声道 wav。env：
  ASR_API_KEY          必填才激活（其余时间纯降级）
  ASR_BASE_URL         默认 https://open.bigmodel.cn/api/paas/v4/audio/transcriptions
  ASR_MODEL            默认 glm-asr-2512
"""
import os
import subprocess
import tempfile

import httpx
from imageio_ffmpeg import get_ffmpeg_exe
from pypinyin import lazy_pinyin, Style

MAX_AUDIO_BYTES = 1_500_000        # 1.5MB 上限：词跟读窗 ~4s aac/webm 远小于此


_CONFU_SM = {"zh": "z", "ch": "c", "sh": "s"}     # 平翘舌
_CONFU_TAIL = {"ang": "an", "eng": "en", "ing": "in"}  # 前后鼻音


def _canon(syl: str) -> str:
    """幼儿音系等价归一：平翘舌/前后鼻音/n·l·r/f·h 视为同音。
    幼儿声母习得晚，"三"读"山"、"牛奶"读"流来"属发音近似不算错；
    乱喊（啊/哦）不落在任何等价类，仍拒。（2026-11-02 用户拍板放宽）"""
    s = syl.replace("ü", "v")
    for k, v in _CONFU_SM.items():
        if s.startswith(k):
            s = v + s[len(k):]
            break
    if s and s[0] in "nr":
        s = "l" + s[1:]
    if s and s[0] == "f":
        s = "h" + s[1:]
    for k, v in _CONFU_TAIL.items():
        if s.endswith(k):
            s = s[:-len(k)] + v
            break
    return s


def _syl(text):
    """声调+音系等价的音节序列（非汉字字符跳过）"""
    return [_canon(s) for s in lazy_pinyin(text, style=Style.NORMAL, errors="ignore")]


def _subseq(needle, hay):
    """needle 是否为 hay 的（非连续）子序列——容忍孩子带前后缀/重复（"天空的天 空"）"""
    it = iter(hay)
    return all(n in it for n in needle)


def _ed_le1(a, b):
    """音节序列编辑距离 ≤1（读漏/读错一个音节仍算对——"正确性不能太高"）"""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    if len(a) > len(b):
        a, b = b, a                       # b 比 a 长 1：a 删一位能与 b 对齐即距离 1
    i = j = 0
    skipped = False
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


def word_match(recognized: str, target: str) -> bool:
    """低门槛内容匹配：文本包含 / 拼音子序列 / 滑窗编辑距离 ≤1 任一命中。
    同音错字（下→夏）与音系近似（平翘舌/前后鼻音/n·l·r/f·h）在拼音层天然算对。
    注：ED 上限保持 1——双字词放开到 2 即两音节全错也过，判错能力归零。"""
    if not recognized or not target:
        return False
    if target in recognized:
        return True
    rs, ts = _syl(recognized), _syl(target)
    if not ts or not rs:
        return False
    if rs == ts or _subseq(ts, rs):
        return True
    if len(ts) <= len(rs):                # 识别带前后缀时按目标长度滑窗比对
        for i in range(0, len(rs) - len(ts) + 1):
            if _ed_le1(ts, rs[i:i + len(ts)]):
                return True
    return False


def _to_wav(raw: bytes) -> bytes | None:
    """浏览器格式（webm/mp4）转 16k 单声道 wav。失败返回 None（降级放行）。"""
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "rec.in"), os.path.join(d, "rec.wav")
        with open(src, "wb") as f:               # ffmpeg 按内容探测容器，无需扩展名
            f.write(raw)
        try:
            p = subprocess.run(
                [get_ffmpeg_exe(), "-v", "error", "-i", src, "-ar", "16000", "-ac", "1", dst],
                capture_output=True, timeout=10)
        except Exception:
            return None
        if p.returncode != 0:
            return None
        with open(dst, "rb") as f:
            return f.read()


def _transcribe(raw: bytes, mime: str) -> str | None:
    """OpenAI 兼容转写。任何异常返回 None（调用方降级放行）。"""
    key = os.environ.get("ASR_API_KEY")
    if not key:
        return None
    m = mime or ""
    if "wav" not in m and "mpeg" not in m and "mp3" not in m:
        raw = _to_wav(raw)
        if raw is None:
            return None
        m = "audio/wav"
    ext = "wav" if "wav" in m else "mp3"
    r = httpx.post(
        os.environ.get("ASR_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions"),
        headers={"Authorization": f"Bearer {key}"},
        files={"file": (f"rec.{ext}", raw, m)},
        data={"model": os.environ.get("ASR_MODEL", "glm-asr-2512")},
        timeout=6,
    )
    r.raise_for_status()
    return (r.json() or {}).get("text") or ""


def verify_audio(raw: bytes, mime: str, target: str) -> dict:
    """转写+匹配。失败一律 {pass: True, degraded: True} —— 判定永不卡孩子。"""
    if not raw or len(raw) > MAX_AUDIO_BYTES:
        return {"pass": True, "text": None, "degraded": True}
    try:
        text = _transcribe(raw, mime)
    except Exception:
        return {"pass": True, "text": None, "degraded": True}
    if text is None:                       # 未配置 key
        return {"pass": True, "text": None, "degraded": True}
    return {"pass": word_match(text, target), "text": text, "degraded": False}
