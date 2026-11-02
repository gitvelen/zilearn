#!/usr/bin/env python3
"""asr.py 三层测试：word_match 纯逻辑 / _to_wav 转码 / verify_audio 端到端（联网，需 ASR_API_KEY）。

跑法：python3 tests/asr_test.py   （无 ASR_API_KEY 时第三层自动 skip）
端到端样例音频：tests 会现场用 ffmpeg 生成"天空"webm（模拟 Chrome 前端上传）。
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import asr  # noqa: E402

KEY = os.environ.get("ASR_API_KEY")


class WordMatchTest(unittest.TestCase):
    def test_exact_contain(self):
        self.assertTrue(asr.word_match("我在读天空这个词", "天空"))

    def test_homophone(self):                      # 同音错字（下→夏）拼音层算对
        self.assertTrue(asr.word_match("夏天天很热", "下"))

    def test_tone_tolerant(self):                  # 声调容错（tiān/tiǎn）
        self.assertTrue(asr.word_match("填空", "天空"))

    def test_wrong_word(self):
        self.assertFalse(asr.word_match("我要吃苹果", "天空"))

    def test_phonological_flat_retroflex(self):   # 平翘舌：三→山
        self.assertTrue(asr.word_match("山水", "三水"))

    def test_phonological_n_l_r(self):            # n/l/r 等价：牛奶→流来
        self.assertTrue(asr.word_match("流来", "牛奶"))

    def test_phonological_f_h(self):              # f/h：飞机→灰机
        self.assertTrue(asr.word_match("灰机", "飞机"))

    def test_phonological_nasal_tail(self):       # 前后鼻音：星星→心心
        self.assertTrue(asr.word_match("心心", "星星"))

    def test_confusion_still_rejects_unrelated(self):  # 等价类不吞无关词
        self.assertFalse(asr.word_match("大地", "天空"))  # ED2 判错能力保留
        self.assertFalse(asr.word_match("苏慧", "山水"))  # 单音节等价救不了双错

    def test_garbage(self):                        # "啊——"乱喊不能过
        self.assertFalse(asr.word_match("啊啊啊啊", "天空"))
        self.assertFalse(asr.word_match("", "天空"))
        self.assertFalse(asr.word_match("天空", ""))


class ToWavTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        src = os.path.join(self.d.name, "t.mp3")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
             src], check=True)
        with open(src, "rb") as f:
            self.mp3 = f.read()

    def test_mp3_to_wav(self):
        wav = asr._to_wav(self.mp3)
        self.assertIsNotNone(wav)
        self.assertEqual(wav[:4], b"RIFF")         # wav 头
        self.assertGreater(len(wav), 4000)         # 0.3s×16k×2B ≈ 9.6KB

    def test_garbage_returns_none(self):
        self.assertIsNone(asr._to_wav(b"not audio at all"))


@unittest.skipUnless(KEY, "ASR_API_KEY 未配置（线上为纯降级模式）")
class VerifyAudioE2E(unittest.TestCase):
    """端到端：webm（Chrome 前端格式）→ 转码 → 智谱识别 → 拼音匹配。"""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, "t.wav")
            webm = os.path.join(d, "t.webm")
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=0.1", wav], check=True)
            # 语音用 edge-tts 现造（无网络则本层 fail，属环境问题）
            mp3 = os.path.join(d, "t.mp3")
            subprocess.run(["edge-tts", "--voice", "zh-CN-XiaoxiaoNeural",
                            "--rate=-20%", "--text", "天空", "--write-media", mp3],
                           check=True, capture_output=True)
            subprocess.run(["ffmpeg", "-v", "error", "-i", mp3,
                            "-c:a", "libopus", webm], check=True)
            with open(webm, "rb") as f:
                cls.webm = f.read()

    def test_webm_correct_word(self):
        r = asr.verify_audio(self.webm, "audio/webm", "天空")
        self.assertEqual(r["degraded"], False)
        self.assertEqual(r["pass"], True)
        self.assertEqual(r["text"], "天空")

    def test_webm_wrong_word(self):
        r = asr.verify_audio(self.webm, "audio/webm", "学校")
        self.assertEqual(r["degraded"], False)
        self.assertEqual(r["pass"], False)         # 识别"天空"≠目标"学校"


if __name__ == "__main__":
    unittest.main(verbosity=2)
