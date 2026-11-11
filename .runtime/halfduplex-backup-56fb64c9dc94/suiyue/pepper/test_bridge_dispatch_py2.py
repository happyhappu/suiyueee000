# -*- coding: utf-8 -*-
"""Exercise Python 2 bridge IPC permissions without connecting to Pepper."""
from __future__ import print_function
import json
import os
import sys
import threading
import unittest

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'naoqi_bridge')
if BRIDGE not in sys.path:
    sys.path.insert(0, BRIDGE)
import bridge


class FakePepper(object):
    def __init__(self, allow_gestures=True):
        self.audio_only = True
        self.allow_gestures = allow_gestures
        self.calls = []
        self.called = threading.Event()

    def safe_gesture(self, params):
        self.calls.append(('safe_gesture', params))
        self.called.set()
        return {'played': True}

    def audio_stop(self, params):
        self.calls.append(('audio_stop', params))
        return {'audio': 'stopped'}


class FakeHandler(bridge._Handler):
    def __init__(self, pepper):
        self.server = type('Server', (object,), {'pepper': pepper})()
        self.sent = []

    def _send(self, result):
        self.sent.append(result)


class DispatchTests(unittest.TestCase):
    def test_only_audio_and_explicit_safe_gestures_are_allowed(self):
        pepper = FakePepper(True)
        h = FakeHandler(pepper)
        for action in ('approach', 'navigate', 'posture', 'emote', 'tablet_render'):
            bridge._Handler._dispatch(h, json.dumps({'id': action, 'action': action}).encode('utf-8'))
        self.assertEqual(len(h.sent), 5)
        self.assertTrue(all('error' in x for x in h.sent))
        bridge._Handler._dispatch(h, json.dumps({'id': 1, 'action': 'safe_gesture',
                                               'params': {'animation': 'wave'}}).encode('utf-8'))
        self.assertTrue(pepper.called.wait(1))
        self.assertEqual(pepper.calls, [('safe_gesture', {'animation': 'wave'})])

    def test_safe_gestures_require_explicit_enable(self):
        pepper = FakePepper(False)
        h = FakeHandler(pepper)
        bridge._Handler._dispatch(h, json.dumps({'id': 1, 'action': 'safe_gesture'}).encode('utf-8'))
        self.assertEqual(len(h.sent), 1)
        self.assertIn('error', h.sent[0])
        self.assertEqual(pepper.calls, [])


if __name__ == '__main__':
    unittest.main()
