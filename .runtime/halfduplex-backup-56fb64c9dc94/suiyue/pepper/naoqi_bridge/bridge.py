# -*- coding: utf-8 -*-
r"""
Pepper 端侧 qi-bridge 主入口（Python 2.7 运行）。

依赖：pynaoqi-python2.7-2.5.7.1-win32-vs2013（import qi / import naoqi，Py2.7-only）
运行前（Windows）：
    set PYTHONPATH=<sdk>\lib
    set PATH=<sdk>\bin;<sdk>\lib;%PATH%
    python2.7 bridge.py --qi-url tcp://<pepper-ip>:9559

职责：
    1. 连 Pepper broker(:9559) 获取各 NAOqi proxy；
    2. 监听本地 TCP（127.0.0.1:9560），把云端（Py3 主网关 suiyue-pepper）下发的
       {action, params} 派发到 self.pepper.*（动作/导航/平板/急停/状态）；
    3. 把麦克风音频帧、在场感知事件回传主网关；
    4. 失联看门狗：主网关心跳超时 -> 自动 stop_all（ADR-011 §决策3）。

本桥只作协议/动作桥，不含任何 LLM/ASR/TTS。Pepper 本体作纯瘦客户端。

⚠️ 本文件按 NAOqi 2.5 培训文档接口形状编写，未经 NAOqi 2.5.10.7 真机验证；
   真机若某 proxy `service not found`，按 pepper_v1.yaml 降级处理。
"""
from __future__ import print_function, absolute_import

import sys
import os
import json
import base64
import time
import signal
import argparse
import threading

try:
    import SocketServer as socketserver   # Py2
except ImportError:
    import socketserver                   # Py3

try:
    import Queue as queue                # Py2
except ImportError:
    import queue                         # Py3

import config as cfg
import qi  # pynaoqi qi

from proxies import PepperProxies
from motion import MotionController
from navigation import NavigationController
from tablet import TabletController
from vision import PerceptionController
import audio as audio_mod


# --------------------------------------------------------------------------
# Pepper 实体：聚合各控制器，暴露云端动作原语
# --------------------------------------------------------------------------
class Pepper(object):
    """self.pepper：云端/MCP 侧看到的实体。所有方法接受 params(dict)。"""

    def __init__(self, session, qi_url, audio_only=False, allow_gestures=False):
        self.audio_only = audio_only
        self.allow_gestures = allow_gestures
        self.session = session
        self.qi_url = qi_url
        self.pepper_ip = _parse_pepper_ip(qi_url)
        self.proxies = PepperProxies(session)
        self.motion = MotionController(self.proxies)
        self.navigation = NavigationController(self.proxies)
        self.tablet = TabletController(self.proxies)
        self.vision = PerceptionController(self.proxies)
        self._audio_running = False
        self._vision_running = False

    # ---- 动作原语（对齐 pepper_v1.yaml action_primitives）----
    def emote(self, params):
        return self.motion.emote(params.get("animation", "wave"), params)

    def safe_gesture(self, params):
        if not self.allow_gestures:
            raise RuntimeError("Expression gestures are disabled")
        return self.motion.safe_gesture(params.get("animation"))

    def approach(self, params):
        return self.motion.approach(params.get("distance"), params)

    def navigate(self, params):
        goal = params.get("goal")
        if goal is None:
            goal = params.get("node")   # MCP-CONTRACT 用 node 表示拓扑节点
        return self.navigation.navigate(goal, params.get("speed"), params)

    def patrol(self, params):
        return self.navigation.patrol(
            params.get("points"), params.get("loops", 1),
            params.get("speed"), params)

    def tablet_render(self, params):
        return self.tablet.render(params.get("url"), params)

    def tablet_clear(self, params):
        return self.tablet.clear(params)

    def posture(self, params):
        return self.motion.posture(params.get("name", "Stand"), params)

    def head_nod(self, params):
        return self.motion.head_nod(params)

    def head_shake(self, params):
        return self.motion.head_shake(params)

    def get_status(self, params):
        return self.motion.get_status(params)

    def stop_all(self, params):
        # 先退订音频/视觉，再急停
        if self._audio_running:
            try:
                audio_mod.stop_audio_stream()
            except Exception:
                pass
            self._audio_running = False
        if self._vision_running:
            try:
                self.vision.stop(params)
            except Exception:
                pass
            self._vision_running = False
        try:
            tts = self.proxies.get("ALTextToSpeech")
            if tts is not None:
                tts.stopAll()
        except Exception:
            pass
        if self.audio_only:
            if self.allow_gestures:
                try:
                    self.motion.stop_safe_gesture()
                except Exception as exc:
                    print("[motion] gesture cancel failed: %s" % exc)
            return {"stopped": True, "audio_only": True}
        return self.motion.stop_all(params)

    # ---- 音频/视觉订阅（由 bridge 的 handler 传入 on_frame/on_event）----
    def audio_start(self, on_frame, params):
        audio_mod.start_audio_stream(self.qi_url, on_frame)
        self._audio_running = True
        return {"audio": "started"}

    def audio_stop(self, params):
        audio_mod.stop_audio_stream()
        self._audio_running = False
        return {"audio": "stopped"}

    def audio_play(self, params):
        # 云端 GPT-SoVITS TTS 音频（PCM）经 Pepper 扬声器放出
        data_b64 = params.get("data_b64")
        if not data_b64:
            return {"error": "no data_b64"}
        pcm = base64.b64decode(data_b64)
        rate = params.get("rate", 16000)
        channels = params.get("channels", 1)
        return audio_mod.play_audio(pcm, rate, channels, self.session)

    def say_text(self, params):
        """让 Pepper 使用机载 ALTextToSpeech 直接朗读文本。"""
        text = params.get("text") or params.get("content") or ""
        text = text.strip()
        if not text:
            return {"spoken": False, "reason": "empty text"}
        tts = self.proxies.require("ALTextToSpeech")
        language = params.get("language") or cfg.TTS_LANGUAGE
        if language:
            try:
                tts.setLanguage(language)
            except Exception as exc:
                print("[tts] setLanguage(%s) failed: %s" % (language, exc))
        try:
            tts.setParameter("speed", int(params.get("speed", cfg.TTS_SPEED)))
        except Exception:
            pass
        tts.say(text)
        return {"spoken": True, "text": text, "language": language}

    def vision_start(self, on_event, params):
        res = self.vision.start(on_event, params)
        self._vision_running = True
        return res

    def vision_stop(self, params):
        res = self.vision.stop(params)
        self._vision_running = False
        return res


# --------------------------------------------------------------------------
# 工具：从 qi_url 解析 Pepper IP（audio_play 用 ALProxy 连接）
# --------------------------------------------------------------------------
def _parse_pepper_ip(qi_url):
    """tcp://192.168.31.x:9559 -> 192.168.31.x"""
    try:
        return qi_url.split("://", 1)[1].split(":")[0]
    except Exception:
        return "127.0.0.1"


# --------------------------------------------------------------------------
# 本地 IPC 服务器
# --------------------------------------------------------------------------
class BridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, pepper, watchdog_timeout=cfg.WATCHDOG_TIMEOUT_S):
        socketserver.ThreadingTCPServer.__init__(self, addr, _Handler)
        self.pepper = pepper
        self.clients = set()
        self.last_heartbeat = time.time()
        self.watchdog_timeout = watchdog_timeout
        self._shutdown_ev = threading.Event()
        self._start_watchdog()

    def note_heartbeat(self):
        self.last_heartbeat = time.time()

    def _start_watchdog(self):
        def loop():
            while not self._shutdown_ev.is_set():
                time.sleep(cfg.HEARTBEAT_INTERVAL_S)
                if time.time() - self.last_heartbeat > self.watchdog_timeout:
                    print("[watchdog] 主网关心跳超时 %ss -> 自动 stop_all"
                          % self.watchdog_timeout)
                    try:
                        self.pepper.stop_all({})
                    except Exception as exc:
                        print("[watchdog] stop_all 异常: %s" % exc)
                    self.last_heartbeat = time.time()  # 避免反复触发
        t = threading.Thread(target=loop)
        t.daemon = True
        t.start()

    def shutdown(self):
        self._shutdown_ev.set()
        try:
            socketserver.ThreadingTCPServer.shutdown(self)
        except Exception:
            pass


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        server = self.server
        server.clients.add(self)
        self.audio_queue = queue.Queue()
        self.event_queue = queue.Queue()
        self._audio_thread = None
        self._event_thread = None
        self._wlock = threading.Lock()
        try:
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                server.note_heartbeat()
                self._dispatch(line)
        except Exception as exc:
            print("[handler] 连接异常: %s" % exc)
        finally:
            self._stop_senders()
            server.clients.discard(self)

    # ---- 派发 ----
    def _dispatch(self, line):
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            self._send({"error": "bad json"})
            return
        action = msg.get("action")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if action in ("ping", "heartbeat"):
            self._send({"id": mid, "ok": True, "result": {"pong": True}})
            return
        try:
            pepper = self.server.pepper
            audio_actions = ("audio_start", "audio_stop", "audio_play", "say_text", "stop_all", "get_status")
            allowed_gesture = action == "safe_gesture" and pepper.allow_gestures
            if pepper.audio_only and action not in audio_actions and not allowed_gesture:
                raise RuntimeError("Action disabled in audio-only mode: %s" % action)
            if action == "audio_start":
                result = pepper.audio_start(self._make_audio_cb(), params)
                # 发送线程在首个音频帧入队时惰性启动；此处不放入哨兵
                self._send({"id": mid, "ok": True, "result": result})
            elif action == "audio_stop":
                result = pepper.audio_stop(params)
                self.audio_queue.put(None)   # 通知 sender 线程退出
                self._send({"id": mid, "ok": True, "result": result})
            elif action == "audio_play":
                def playback():
                    try:
                        result = pepper.audio_play(params)
                        self._send({"id": mid, "ok": True, "result": result})
                    except Exception as exc:
                        self._send({"id": mid, "error": str(exc)})
                worker = threading.Thread(target=playback)
                worker.daemon = True
                worker.start()
            elif action == "say_text":
                def speech():
                    try:
                        result = pepper.say_text(params)
                        self._send({"id": mid, "ok": True, "result": result})
                    except Exception as exc:
                        self._send({"id": mid, "error": str(exc)})
                worker = threading.Thread(target=speech)
                worker.daemon = True
                worker.start()
            elif action == "safe_gesture":
                def gesture():
                    try:
                        result = pepper.safe_gesture(params)
                        self._send({"id": mid, "ok": True, "result": result})
                    except Exception as exc:
                        self._send({"id": mid, "error": str(exc)})
                worker = threading.Thread(target=gesture)
                worker.daemon = True
                worker.start()
            elif action == "vision_start":
                result = pepper.vision_start(self._make_event_cb(), params)
                self._send({"id": mid, "ok": True, "result": result})
            elif action == "vision_stop":
                result = pepper.vision_stop(params)
                self.event_queue.put(None)
                self._send({"id": mid, "ok": True, "result": result})
            elif hasattr(pepper, action):
                fn = getattr(pepper, action)
                result = fn(params)
                self._send({"id": mid, "ok": True, "result": result})
            else:
                self._send({"id": mid, "error": "unknown action: %s" % action})
        except Exception as exc:
            self._send({"id": mid, "error": "%s: %s"
                        % (type(exc).__name__, exc)})

    # ---- 音频/事件回调 -> 队列 -> 发送线程 ----
    def _make_audio_cb(self):
        def cb(pcm, rate, channels):
            self.enqueue_audio(pcm, rate, channels)
        return cb

    def _make_event_cb(self):
        def cb(name, payload):
            self.enqueue_event(name, payload)
        return cb

    def enqueue_audio(self, pcm, rate, channels):
        if self._audio_thread is None:
            self._audio_thread = threading.Thread(target=self._audio_sender)
            self._audio_thread.daemon = True
            self._audio_thread.start()
        self.audio_queue.put((pcm, rate, channels))

    def enqueue_event(self, name, payload):
        if self._event_thread is None:
            self._event_thread = threading.Thread(target=self._event_sender)
            self._event_thread.daemon = True
            self._event_thread.start()
        self.event_queue.put((name, payload))

    def _audio_sender(self):
        while True:
            item = self.audio_queue.get()
            if item is None:
                break
            pcm, rate, channels = item
            try:
                self._send({
                    "type": "audio",
                    "rate": rate,
                    "channels": channels,
                    "data_b64": base64.b64encode(pcm).decode("ascii"),
                })
            except Exception:
                break

    def _event_sender(self):
        while True:
            item = self.event_queue.get()
            if item is None:
                break
            name, payload = item
            try:
                self._send({"type": "event", "name": name, "data": payload})
            except Exception:
                break

    def _stop_senders(self):
        if self._audio_thread is not None:
            self.audio_queue.put(None)
        if self._event_thread is not None:
            self.event_queue.put(None)

    def _send(self, obj):
        try:
            with self._wlock:
                self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
                self.wfile.flush()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Pepper qi-bridge (Py2.7)")
    ap.add_argument("--qi-url",
                    default=os.environ.get("PEPPER_QI_URL", cfg.DEFAULT_QI_URL))
    ap.add_argument("--ipc-host", default=cfg.DEFAULT_IPC_HOST)
    ap.add_argument("--ipc-port", type=int, default=cfg.DEFAULT_IPC_PORT)
    ap.add_argument("--watchdog", type=float, default=cfg.WATCHDOG_TIMEOUT_S,
                    help="主网关心跳超时秒数（> 则自动 stop_all）")
    ap.add_argument("--audio-only", action="store_true", help="Audio IPC only, except explicitly enabled safe gestures")
    ap.add_argument("--allow-gestures", action="store_true", help="Allow only whitelisted expression animations")
    args = ap.parse_args()

    if sys.version_info[0] != 2:
        raise RuntimeError(
            "naoqi_bridge 必须用 Python 2.7 运行（pynaoqi 2.5.7.1 仅 Py2.7）")

    session = qi.Session()
    session.connect(args.qi_url)
    print("[bridge] 已连接 Pepper broker: %s" % args.qi_url)

    pepper = Pepper(session, args.qi_url, audio_only=args.audio_only,
                    allow_gestures=args.allow_gestures)
    print("[bridge] proxy 可用性: %s" % json.dumps(pepper.proxies.summary()))

    server = BridgeServer((args.ipc_host, args.ipc_port), pepper,
                          watchdog_timeout=args.watchdog)
    print("[bridge] 本地 IPC 监听 %s:%d（Py3 主网关连此端口）"
          % (args.ipc_host, args.ipc_port))

    def _shutdown(signum, frame):
        print("[bridge] 收到信号 %s，正在关闭..." % signum)
        try:
            pepper.stop_all({})
        except Exception:
            pass
        # shutdown() must run outside the serve_forever thread.
        closer = threading.Thread(target=server.shutdown)
        closer.daemon = True
        closer.start()

    try:
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except ValueError:
        pass  # 非主线程时不支持信号，忽略

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            pepper.stop_all({})
        except Exception:
            pass
        server.shutdown()
        print("[bridge] 已停止")


if __name__ == "__main__":
    main()
