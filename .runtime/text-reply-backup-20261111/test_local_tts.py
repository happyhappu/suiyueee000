"""Tests for sending reply text to Pepper's local ALTextToSpeech."""
from __future__ import annotations

import threading
import os
import sys
import unittest
from types import SimpleNamespace

_PEPPER_DIR = os.path.dirname(os.path.abspath(__file__))
if _PEPPER_DIR not in sys.path:
    sys.path.insert(0, _PEPPER_DIR)

from pepper import gateway


class FakeBridge(object):
    def __init__(self):
        self.actions = []
        self.called = threading.Event()

    def send_action(self, action, params=None, timeout=10.0):
        self.actions.append((action, params, timeout))
        self.called.set()
        return {"id": len(self.actions), "ok": True,
                "result": {"spoken": True, "text": params.get("text", "")}}


class TestLocalTts(unittest.TestCase):
    def setUp(self):
        self.old_local_tts = gateway.cfg.LOCAL_TTS
        gateway.cfg.LOCAL_TTS = True

    def tearDown(self):
        gateway.cfg.LOCAL_TTS = self.old_local_tts

    def make_gateway(self):
        args = SimpleNamespace(
            cloud_ws="ws://unused", device_id="test", ipc_host="127.0.0.1",
            ipc_port=9560, auto_bridge=False, py2_exe="python2.7",
            audio_only=True, test_text=None,
        )
        instance = gateway.PepperGateway(args)
        instance.bridge = FakeBridge()
        return instance

    def test_llm_reply_text_is_sent_to_local_speaker(self):
        instance = self.make_gateway()
        instance._on_cloud_text('{"type":"llm","text":"🙂",'
                                '"content":"你好，我是岁悦。"}')
        self.assertTrue(instance.bridge.called.wait(2))
        self.assertEqual(instance.bridge.actions[0][0], "say_text")
        self.assertEqual(instance.bridge.actions[0][1]["text"], "你好，我是岁悦。")
        instance._stop.set()

    def test_tts_sentence_text_is_used_once_and_audio_is_ignored(self):
        instance = self.make_gateway()
        instance._on_tts_state({"state": "start"})
        instance._on_tts_state({"state": "sentence_start", "text": "今天感觉怎么样？"})
        instance._on_cloud_binary(b"encoded audio")
        instance._on_tts_state({"state": "sentence_start", "text": "今天感觉怎么样？"})
        self.assertTrue(instance.bridge.called.wait(2))
        self.assertEqual(len(instance.bridge.actions), 1)
        self.assertEqual(instance.bridge.actions[0][1]["text"], "今天感觉怎么样？")
        self.assertEqual(instance._tts_chunks, [])
        instance._stop.set()

if __name__ == "__main__":
    unittest.main()
