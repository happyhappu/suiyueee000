"""Half-duplex, emoji display and constrained emotion-to-gesture tests."""
import json
import os
import struct
import sys
import threading
import time
import unittest
from types import SimpleNamespace

_PEPPER_DIR = os.path.dirname(os.path.abspath(__file__))
if _PEPPER_DIR not in sys.path:
    sys.path.insert(0, _PEPPER_DIR)
if os.path.dirname(_PEPPER_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(_PEPPER_DIR))
from pepper import gateway


class FakeSocket:
    def __init__(self):
        self.binary = []
        self.messages = []

    def send(self, message):
        self.messages.append(json.loads(message))

    def send_binary(self, message):
        self.binary.append(message)


class FakeBridge:
    def __init__(self):
        self.actions = []
        self.say_started = threading.Event()
        self.release_speech = threading.Event()
        self.gesture_started = threading.Event()
        self.release_gesture = threading.Event()

    def send_action(self, action, params=None, timeout=10):
        self.actions.append((action, params))
        if action == 'say_text':
            self.say_started.set()
            self.release_speech.wait(2)
            return {'ok': True, 'result': {'spoken': True}}
        if action == 'safe_gesture':
            self.gesture_started.set()
            self.release_gesture.wait(2)
            return {'ok': True, 'result': {'played': True}}
        return {'ok': True}


class TestTurnTaking(unittest.TestCase):
    def setUp(self):
        self.saved = (gateway.cfg.LOCAL_TTS,
                      gateway.cfg.HUMAN_SILENCE_BEFORE_REPLY_S,
                      gateway.cfg.MIC_RESUME_DELAY_S,
                      gateway.cfg.GESTURE_COOLDOWN_S)
        gateway.cfg.LOCAL_TTS = True
        gateway.cfg.HUMAN_SILENCE_BEFORE_REPLY_S = 0.16
        gateway.cfg.MIC_RESUME_DELAY_S = 0.12
        gateway.cfg.GESTURE_COOLDOWN_S = 0.0
        args = SimpleNamespace(cloud_ws='ws://unused', device_id='test',
                               ipc_host='127.0.0.1', ipc_port=9560,
                               auto_bridge=False, py2_exe='python2',
                               audio_only=True, allow_gestures=False, test_text=None)
        self.gw = gateway.PepperGateway(args)
        self.gw.bridge = FakeBridge()
        self.gw.ws = FakeSocket()
        self.gw._cloud_ready.set()
        self.gw._encode_audio = lambda pcm, rate, channels: b'encoded'
        self.loud = struct.pack('<h', 1200) * gateway.cfg.OPUS_FRAME_SAMPLES

    def tearDown(self):
        self.gw.bridge.release_speech.set()
        self.gw.bridge.release_gesture.set()
        self.gw._stop.set()
        (gateway.cfg.LOCAL_TTS,
         gateway.cfg.HUMAN_SILENCE_BEFORE_REPLY_S,
         gateway.cfg.MIC_RESUME_DELAY_S,
         gateway.cfg.GESTURE_COOLDOWN_S) = self.saved

    def test_waits_while_person_speaks_and_mutes_during_and_after_reply(self):
        gw = self.gw
        gw._on_bridge_audio(self.loud, 16000, 1)
        self.assertEqual(len(gw.ws.binary), 1)
        gw._on_cloud_text(json.dumps({'type': 'llm', 'text': '🙂', 'emotion': 'happy',
                                      'content': '你好。', 'turn_id': 'turn-1'}))
        self.assertFalse(gw.bridge.say_started.wait(0.07))
        # Continue speaking while the model's reply is waiting: audio still
        # goes to the brain, and playback's silence timer restarts.
        gw._on_bridge_audio(self.loud, 16000, 1)
        self.assertEqual(len(gw.ws.binary), 2)
        self.assertFalse(gw.bridge.say_started.wait(0.07))
        self.assertTrue(gw.bridge.say_started.wait(0.5))
        self.assertTrue(gw._tts_playing.is_set())
        gw._on_bridge_audio(self.loud, 16000, 1)
        self.assertEqual(len(gw.ws.binary), 2)
        gw.bridge.release_speech.set()
        deadline = time.monotonic() + 1
        while gw._tts_playing.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(gw._tts_playing.is_set())
        gw._on_bridge_audio(self.loud, 16000, 1)
        self.assertEqual(len(gw.ws.binary), 2)  # post-playback echo tail
        time.sleep(0.14)
        gw._on_bridge_audio(self.loud, 16000, 1)
        self.assertEqual(len(gw.ws.binary), 3)
        self.assertEqual([m['state'] for m in gw.ws.messages if m['type'] == 'listen'],
                         ['start', 'start'])

    def test_emoji_and_dialogue_are_visible_in_existing_console(self):
        gw = self.gw
        with self.assertLogs('suiyue.pepper.gateway', level='INFO') as logs:
            gw._on_cloud_text(json.dumps({'type': 'llm', 'text': '🙂',
                                          'emotion': 'happy', 'content': '你好。',
                                          'turn_id': 'display-1'}))
        self.assertTrue(any('🙂' in line and '你好。' in line for line in logs.output))

    def test_emotion_only_event_does_not_read_the_emoji(self):
        gw = self.gw
        gw._on_cloud_text(json.dumps({'type': 'llm', 'text': '🙂', 'emotion': 'happy'}))
        self.assertFalse(gw.bridge.say_started.wait(0.1))

    def test_gesture_is_whitelisted_and_never_blocks_speech(self):
        gw = self.gw
        gw._allow_gestures = True
        gw._on_cloud_text(json.dumps({'type': 'llm', 'text': '🙂', 'emotion': 'happy',
                                      'content': '你好。', 'turn_id': 'gesture-1'}))
        self.assertTrue(gw.bridge.gesture_started.wait(0.6))
        self.assertTrue(gw.bridge.say_started.wait(0.6))
        gesture = [a for a in gw.bridge.actions if a[0] == 'safe_gesture']
        self.assertEqual(gesture, [('safe_gesture', {'animation': 'wave'})])
        self.assertTrue(gw._audio_only)

    def test_unknown_and_neutral_emotions_do_not_move(self):
        gw = self.gw
        gw._allow_gestures = True
        gw._emote_from_emotion('neutral')
        gw._emote_from_emotion('unknown')
        self.assertFalse(gw.bridge.gesture_started.wait(0.1))


if __name__ == '__main__':
    unittest.main()
