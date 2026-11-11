# -*- coding: utf-8 -*-
"""Pepper 主网关（suiyue-pepper，Python 3.13 运行）。

角色：扮演 xiaozhi client 连云端 xiaozhi-esp32-server WS（与 Otto/Reachy 同协议），
      本地 TCP 连 naoqi_bridge（Py2.7 桥）控制 Pepper 硬件。
      把 Pepper 当成云端的一个"对话设备"，云端零改动（端侧 OS 透明原则 D2）。

数据流：
  - Pepper 麦克风音频（桥 -> 本网关，16k PCM）-> 编码(opus/raw) -> xiaozhi WS 上行
  - 云端 TTS 音频（xiaozhi WS binary）-> 解码 PCM -> 桥 audio_play -> Pepper 扬声器
  - 云端 LLM emotion 意图（xiaozhi WS text）-> 映射动作原语 -> 桥执行（self.pepper.*）
  - 云端 STT 文本 -> 日志（可选回显平板，见 TODO）

⚠️ 按 xiaozhi-esp32-server v1 公开协议编写，未经真机验证；
   握手字段、opus 协商、emotion 字段名、listen 状态机等未确认点已在注释标 ⚠️。
   真机出入按模块局部调整即可（已用 ⚠️ 标注所有待确认点）。
"""
from __future__ import annotations

import os
import sys
import json
import base64
import time
import socket
import signal
import argparse
import logging
import queue
import threading
import subprocess
from typing import Optional, Callable

import gateway_config as cfg

log = logging.getLogger("suiyue.pepper.gateway")

try:
    import websocket  # websocket-client
    _HAVE_WS = True
except ImportError:
    _HAVE_WS = False
    log.warning("websocket-client 未安装；运行前请 `pip install websocket-client`")

_OPUS_DLL_DIR_HANDLE = None
if os.name == "nt":
    _workspace_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    _opus_bin = os.environ.get(
        "PEPPER_OPUS_BIN",
        os.path.join(_workspace_root, ".runtime", "libopus", "mingw32", "bin"),
    )
    if os.path.isdir(_opus_bin):
        os.environ["PATH"] = _opus_bin + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            _OPUS_DLL_DIR_HANDLE = os.add_dll_directory(_opus_bin)

try:
    import opuslib
    _HAVE_OPUS = True
    _OPUS_IMPORT_ERROR = None
except Exception as exc:
    _HAVE_OPUS = False
    _OPUS_IMPORT_ERROR = exc


# --------------------------------------------------------------------------
# 本地 IPC client：连 naoqi_bridge(:9560)
# --------------------------------------------------------------------------
class BridgeClient:
    """连 naoqi_bridge 本地 TCP IPC，下发动作原语、接收音频/事件/状态。"""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self._wlock = threading.Lock()
        self._rid = 0
        self._pending: dict = {}
        self._pending_lock = threading.Lock()
        self.on_audio: Optional[Callable[[bytes, int, int], None]] = None
        self.on_event: Optional[Callable[[str, object], None]] = None
        self.on_status: Optional[Callable[[object], None]] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def connect(self, retry_timeout: float = 0.0) -> None:
        deadline = time.monotonic() + retry_timeout
        while True:
            try:
                self.sock = socket.create_connection((self.host, self.port), timeout=2)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
        self.sock.settimeout(None)
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        log.info("已连 naoqi_bridge IPC %s:%d", self.host, self.port)

    def _next_id(self) -> int:
        with self._pending_lock:
            self._rid += 1
            return self._rid

    def send_action(self, action: str, params=None, timeout: float = 10.0):
        """下发动作原语，阻塞等响应（按 id 匹配）。"""
        mid = self._next_id()
        q: "queue.Queue" = queue.Queue()
        with self._pending_lock:
            self._pending[mid] = q
        msg = json.dumps({"id": mid, "action": action, "params": params or {}})
        with self._wlock:
            self.sock.sendall((msg + "\n").encode("utf-8"))
        try:
            return q.get(timeout=timeout)
        finally:
            with self._pending_lock:
                self._pending.pop(mid, None)

    def ping(self):
        return self.send_action("ping", timeout=3.0)

    def _recv_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                data = self.sock.recv(4096)
            except Exception as exc:
                if not self._stop.is_set():
                    log.error("IPC 接收异常: %s", exc)
                break
            if not data:
                if not self._stop.is_set():
                    log.error("IPC 连接关闭")
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    self._handle(line.decode("utf-8"))

    def _handle(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            return
        # 响应（含 id 的 ok/error/result/pong）
        if "id" in msg and ("ok" in msg or "error" in msg or "result" in msg):
            mid = msg.get("id")
            with self._pending_lock:
                q = self._pending.get(mid)
            if q:
                q.put(msg)
            return
        t = msg.get("type")
        if t == "audio" and self.on_audio:
            pcm = base64.b64decode(msg.get("data_b64", ""))
            self.on_audio(pcm, int(msg.get("rate", cfg.AUDIO_RATE)),
                          int(msg.get("channels", cfg.AUDIO_CHANNELS)))
        elif t == "event" and self.on_event:
            self.on_event(msg.get("name", ""), msg.get("data"))
        elif t == "status" and self.on_status:
            self.on_status(msg.get("data"))

    def close(self) -> None:
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Pepper 主网关
# --------------------------------------------------------------------------
class PepperGateway:
    def __init__(self, args):
        self.cloud_ws = args.cloud_ws
        self.device_id = args.device_id
        self.bridge = BridgeClient(args.ipc_host, args.ipc_port)
        self.bridge.on_audio = self._on_bridge_audio
        self.bridge.on_event = self._on_bridge_event
        self.ws = None
        self._session_id: Optional[str] = None
        self._stop = threading.Event()
        self._opus_enc = None
        self._opus_dec = None
        self._listen_active = False
        self._listen_lock = threading.Lock()
        self._silence_since: Optional[float] = None
        self._vad_lock = threading.Lock()
        self._tts_playing = threading.Event()   # TTS 播放时暂停麦克风上行
        self._audio_gate_lock = threading.RLock()
        self._last_human_voice = 0.0
        self._resume_mic_at = 0.0
        self._tts_state = "idle"
        self._tts_chunks: list = []
        self._tts_lock = threading.RLock()
        self._cloud_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._auto_bridge = args.auto_bridge
        self._py2_exe = args.py2_exe
        self._bridge_proc: Optional[subprocess.Popen] = None
        self._audio_only = args.audio_only
        self._allow_gestures = bool(getattr(args, "allow_gestures", False))
        self._gesture_lock = threading.Lock()
        self._gesture_running = False
        self._last_gesture_at = 0.0
        self._gesture_done = threading.Event()
        self._gesture_done.set()
        self._gesture_result = None
        self._test_text = getattr(args, "test_text", None)
        self._test_done = threading.Event()
        self._test_ok = False
        self._cloud_ready = threading.Event()
        self._pcm_pending = bytearray()
        self._play_queue = queue.Queue()
        self._pending_playbacks = 0
        self._speech_queue = queue.Queue()
        self._speech_lock = threading.Lock()
        self._spoken_texts = set()
        self._spoken_turn_ids = {}
        self._pending_speech_count = 0
        threading.Thread(target=self._speech_loop, daemon=True).start()
        if not cfg.LOCAL_TTS:
            threading.Thread(target=self._playback_loop, daemon=True).start()

    # ---------------- 启动 ----------------
    def run(self) -> None:
        try:
            if self._auto_bridge:
                self._start_bridge_subprocess()
            self.bridge.connect(retry_timeout=20.0 if self._auto_bridge else 0.0)
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()
            self._connect_cloud()
            if not self._cloud_ready.wait(25):
                raise RuntimeError("Cloud hello timed out")
            if self._test_text:
                self.ws.send(json.dumps({"type": "listen", "state": "detect", "text": self._test_text}))
                if not self._test_done.wait(90) or not self._test_ok:
                    raise RuntimeError("Text-to-Pepper playback test did not complete")
                if self._allow_gestures:
                    self._gesture_done.wait(30)
                    if self._gesture_result:
                        log.info("EXPRESSION_GESTURE_TEST_PASS")
                log.info("VOICE_DOWNLINK_TEST_PASS")
                return
            result = self.bridge.send_action("audio_start", timeout=20)
            if not result.get("ok"):
                raise RuntimeError("Microphone start failed: %s" % result)
            log.info("Microphone started; audio_only=%s", self._audio_only)
            while not self._stop.wait(0.5):
                stop_file = os.environ.get("PEPPER_STOP_FILE")
                if stop_file and os.path.isfile(stop_file):
                    break
        finally:
            self.shutdown()

    def _connect_cloud(self) -> None:
        if not _HAVE_WS:
            raise RuntimeError("websocket-client 未安装，无法连云端")
        if cfg.AUDIO_FORMAT == "opus" and not _HAVE_OPUS:
            raise RuntimeError("AUDIO_FORMAT=opus 但 Opus 运行库不可用: %s" %
                               _OPUS_IMPORT_ERROR)
        self.ws = websocket.create_connection(
            self.cloud_ws, enable_multithread=True, timeout=20,
            header={"Device-Id": self.device_id, "Client-Id": "pepper-gateway"},
            http_no_proxy=["192.168.0.87", "127.0.0.1", "localhost"],
        )
        self.ws.settimeout(None)
        hello = {
            "type": "hello",
            "version": 1,
            "features": {"emoji": True},
            "transport": "websocket",
            "audio_params": {
                # ⚠️ format 字段：xiaozhi-server 通常要求 opus；若 server 支持 raw 可改
                "format": cfg.AUDIO_FORMAT,
                "sample_rate": cfg.AUDIO_RATE,
                "channels": cfg.AUDIO_CHANNELS,
                "frame_duration": cfg.AUDIO_FRAME_DURATION_MS,
            },
            # ⚠️ 部分 server 版本接受 board/device_id 标识设备；留空让 server 按连接分配
        }
        self.ws.send(json.dumps(hello))
        log.info("已发送 hello 握手 -> %s", self.cloud_ws)
        self._cloud_thread = threading.Thread(target=self._cloud_recv_loop, daemon=True)
        self._cloud_thread.start()

    def _cloud_recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                opcode, data = self.ws.recv_data(True)
            except Exception as exc:
                if not self._stop.is_set():
                    log.error("云端 WS 断开: %s", exc)
                self._cloud_ready.clear()
                self._listen_active = False
                with self._tts_lock:
                    self._tts_chunks = []
                    self._tts_state = "idle"
                    with self._speech_lock:
                        if not self._pending_playbacks and not self._pending_speech_count:
                            self._tts_playing.clear()
                while not self._stop.wait(3):
                    try:
                        self._connect_cloud()
                        return
                    except Exception as reconnect_error:
                        log.warning("Cloud reconnect failed: %s", reconnect_error)
                return
            if opcode == websocket.ABNF.OPCODE_TEXT:
                self._on_cloud_text(data.decode("utf-8", "replace"))
            elif opcode == websocket.ABNF.OPCODE_BINARY:
                self._on_cloud_binary(data)

    def _on_cloud_text(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        mtype = msg.get("type")
        if mtype == "hello":
            self._session_id = msg.get("session_id")
            self._cloud_ready.set()
            ap = msg.get("audio_params") or {}
            log.info("云端 hello 回包 session_id=%s audio_params=%s",
                     self._session_id, ap)
        elif mtype == "stt":
            log.info("[STT] %s", msg.get("text", ""))
        elif mtype == "llm":
            # ⚠️ emotion 字段名依 server/LLM 版本；兼容直接 emotion 或嵌套 llm.emotion
            emotion = msg.get("emotion") or (msg.get("llm") or {}).get("emotion")
            raw_text = msg.get("text") or ""
            emoji = (raw_text if raw_text in cfg.SERVER_EMOTION_TO_EMOJI.values()
                     else cfg.SERVER_EMOTION_TO_EMOJI.get(emotion, ""))
            content = msg.get("content") or (
                "" if emoji == raw_text or raw_text in cfg.SERVER_EMOTION_TO_SUYUE else raw_text
            )
            if content:
                # start-voice.cmd's console is the existing conversation popup.
                # `text` is the emoji; only `content` is sent to local TTS.
                log.info("[LLM] %s %s", emoji, content)
                if cfg.LOCAL_TTS:
                    self._enqueue_speech(content, turn_id=msg.get("turn_id"),
                                         emotion=emotion)
            elif emoji:
                log.info("[表情] %s (%s)", emoji, emotion or "unknown")
            if emotion and not cfg.LOCAL_TTS and self._allow_gestures:
                self._emote_from_emotion(emotion)
        elif mtype == "tts":
            self._on_tts_state(msg)
        else:
            log.debug("云端消息 type=%s: %s", mtype, text[:120])

    def _on_tts_state(self, msg: dict) -> None:
        state = msg.get("state")
        with self._tts_lock:
            if state == "start":
                self._tts_state = "streaming"
                self._tts_chunks = []
                if not cfg.LOCAL_TTS:
                    self._tts_playing.set()
            elif state == "sentence_start":
                text = msg.get("text") or msg.get("content") or ""
                log.info("[TTS text] %s", text)
                if cfg.LOCAL_TTS:
                    self._enqueue_speech(text)
            elif state in ("stop", "finish"):
                if cfg.LOCAL_TTS:
                    self._tts_state = "idle"
                    with self._speech_lock:
                        self._spoken_texts.clear()
                    return
                if self._tts_state == "streaming":
                    self._flush_tts()
                self._tts_state = "idle"
                if not self._pending_playbacks:
                    self._tts_playing.clear()

    def _flush_tts(self) -> None:
        with self._tts_lock:
            chunks, self._tts_chunks = self._tts_chunks, []
            if chunks:
                self._pending_playbacks += 1
                self._play_queue.put(chunks)

    def _playback_loop(self) -> None:
        while True:
            chunks = self._play_queue.get()
            if chunks is None:
                return
            try:
                decoded = [self._decode_audio(packet) for packet in chunks]
                if any(part is None for part in decoded):
                    raise RuntimeError("One or more Opus packets failed to decode")
                pcm = b"".join(decoded)
                result = self.bridge.send_action("audio_play", {
                    "data_b64": base64.b64encode(pcm).decode("ascii"),
                    "rate": cfg.AUDIO_RATE, "channels": cfg.AUDIO_CHANNELS,
                }, timeout=max(30.0, len(pcm) / 32000.0 + 15))
                if not result.get("ok") or not result.get("result", {}).get("played"):
                    raise RuntimeError("Pepper rejected playback: %s" % result)
                log.info("[TTS] Playback confirmed: %s", result["result"])
                self._test_ok = True
            except Exception:
                log.exception("TTS playback failed")
            finally:
                self._test_done.set()
                with self._tts_lock:
                    self._pending_playbacks -= 1
                    if self._pending_playbacks == 0 and self._tts_state == "idle":
                        self._tts_playing.clear()
                self._listen_active = False
                self._pcm_pending.clear()

    def _on_cloud_binary(self, data: bytes) -> None:
        if cfg.LOCAL_TTS:
            return
        with self._tts_lock:
            if self._tts_state == "streaming":
                self._tts_chunks.append(data)

    def _enqueue_speech(self, text: str, turn_id=None, emotion=None) -> bool:
        text = " ".join(str(text).split()).strip()
        # Emoji-only legacy events have no turn_id. Complete text-mode
        # messages do, so legitimate one-character replies are accepted.
        if not text or (not turn_id and len(text) <= 1):
            return False
        with self._speech_lock:
            if turn_id:
                turn_id = str(turn_id)
                if turn_id in self._spoken_turn_ids:
                    return False
                self._spoken_turn_ids[turn_id] = None
                if len(self._spoken_turn_ids) > 256:
                    self._spoken_turn_ids.pop(next(iter(self._spoken_turn_ids)))
            else:
                # Keep the original tts.stop-based deduplication for old servers.
                if text in self._spoken_texts:
                    return False
                self._spoken_texts.add(text)
            self._pending_speech_count += 1
            self._speech_queue.put((text, emotion))
        return True

    def _wait_for_user_pause(self) -> bool:
        """Start local playback only after the speaker has stopped."""
        while not self._stop.is_set():
            with self._audio_gate_lock:
                now = time.monotonic()
                if self._tts_playing.is_set():
                    return True  # another queued reply kept the gate closed
                if (now >= self._resume_mic_at and
                        now - self._last_human_voice >= cfg.HUMAN_SILENCE_BEFORE_REPLY_S):
                    self._tts_playing.set()
                    self._pcm_pending.clear()
                    return True
            self._stop.wait(0.05)
        return False

    def _speech_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._speech_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return
            text, emotion = item
            try:
                if not self._wait_for_user_pause():
                    return
                if emotion and self._allow_gestures:
                    self._emote_from_emotion(emotion)
                result = self.bridge.send_action(
                    "say_text", {"text": text},
                    timeout=max(20.0, len(text) * 1.5 + 5.0),
                )
                if not result.get("ok"):
                    log.error("本机 TTS 失败: %s", result)
                else:
                    log.info("[Local TTS] %s", text)
                    self._test_ok = True
            except Exception:
                log.exception("本机 TTS 下发失败")
            finally:
                self._test_done.set()
                with self._speech_lock:
                    self._pending_speech_count = max(0, self._pending_speech_count - 1)
                    with self._audio_gate_lock:
                        # Clear frames from before/during playback. The next
                        # listen start resets the brain's partial VAD buffer.
                        self._pcm_pending.clear()
                        self._listen_active = False
                        self._resume_mic_at = time.monotonic() + cfg.MIC_RESUME_DELAY_S
                        self._last_human_voice = 0.0
                        if not self._pending_speech_count:
                            self._tts_playing.clear()

    # ---------------- 上行：Pepper 麦克风 -> 云端 ----------------
    def _on_bridge_audio(self, pcm: bytes, rate: int, channels: int) -> None:
        with self._audio_gate_lock:
            now = time.monotonic()
            if (self._tts_playing.is_set() or now < self._resume_mic_at
                    or not self._cloud_ready.is_set()):
                self._pcm_pending.clear()
                return
            if rate != cfg.AUDIO_RATE or channels != cfg.AUDIO_CHANNELS:
                log.error("Unexpected microphone format: %s/%s", rate, channels)
                return
            if _rms_int16(pcm) >= cfg.VAD_ENERGY_THRESHOLD:
                self._last_human_voice = now
            self._pcm_pending.extend(pcm)
            size = cfg.OPUS_FRAME_SAMPLES * channels * 2
            while len(self._pcm_pending) >= size:
                pcm_frame = bytes(self._pcm_pending[:size])
                del self._pcm_pending[:size]
                if not self._ensure_listen(pcm_frame):
                    continue
                frame = self._encode_audio(pcm_frame, rate, channels)
                if frame is not None and self.ws is not None:
                    self.ws.send_binary(frame)

    def _ensure_listen(self, pcm: bytes) -> bool:
        """Let the brain's Silero VAD detect utterance boundaries, including silence."""
        if not self._listen_active:
            self._send_listen("start")
            self._listen_active = True
        return True

    def _send_listen(self, state: str) -> None:
        if self.ws is None:
            return
        msg = {"type": "listen", "state": state, "mode": "auto"}
        if self._session_id:
            msg["session_id"] = self._session_id
        try:
            self.ws.send(json.dumps(msg))
            log.info("listen %s", state)
        except Exception as exc:
            log.error("listen %s 失败: %s", state, exc)

    # ---------------- emotion -> 动作 ----------------
    def _emote_from_emotion(self, emotion: str) -> None:
        """Schedule one whitelisted animation without blocking speech or IPC."""
        if not self._allow_gestures:
            return
        animation = cfg.SERVER_EMOTION_TO_GESTURE.get(emotion)
        if not animation:
            return
        with self._gesture_lock:
            now = time.monotonic()
            if self._gesture_running or now - self._last_gesture_at < cfg.GESTURE_COOLDOWN_S:
                log.info("表情动作忙或冷却中，跳过 %s", emotion)
                return
            self._gesture_running = True
            self._last_gesture_at = now
            self._gesture_result = None
            self._gesture_done.clear()

        def run_gesture():
            try:
                result = self.bridge.send_action(
                    "safe_gesture", {"animation": animation}, timeout=30.0)
                if not result.get("ok") or not result.get("result", {}).get("played"):
                    log.warning("表情动作未执行: %s", result)
                else:
                    self._gesture_result = True
                    log.info("emotion=%s -> 动作=%s result=%s", emotion, animation, result)
            except Exception as exc:
                log.warning("表情动作失败: %s", exc)
            finally:
                with self._gesture_lock:
                    self._gesture_running = False
                    self._gesture_done.set()

        threading.Thread(target=run_gesture, daemon=True).start()

    # ---------------- 感知事件 ----------------
    def _on_bridge_event(self, name: str, data) -> None:
        # ⚠️ FaceDetected/PeoplePerception 可用于主动 approach（老人进入视野）。
        # 当前仅日志；若要"看到老人就走近"，在此触发 bridge.send_action("approach", ...)。
        log.info("[EVENT] %s %s", name, str(data)[:120])

    # ---------------- 编码/解码 ----------------
    def _encode_audio(self, pcm: bytes, rate: int, channels: int) -> Optional[bytes]:
        if cfg.AUDIO_FORMAT == "raw":
            return pcm
        return self._opus_encode(pcm)

    def _decode_audio(self, data: bytes) -> Optional[bytes]:
        if cfg.AUDIO_FORMAT == "raw":
            return data
        return self._opus_decode(data)

    def _opus_encode(self, pcm: bytes) -> Optional[bytes]:
        if not _HAVE_OPUS:
            log.error("opuslib 未装，无法编码；请将 PEPPER_AUDIO_FORMAT=raw")
            return None
        try:
            if self._opus_enc is None:
                self._opus_enc = opuslib.Encoder(
                    cfg.AUDIO_RATE, cfg.AUDIO_CHANNELS, cfg.OPUS_APPLICATION)
            return self._opus_enc.encode(pcm, cfg.OPUS_FRAME_SAMPLES)
        except Exception as exc:
            log.error("opus 编码失败: %s", exc)
            return None

    def _opus_decode(self, data: bytes) -> Optional[bytes]:
        if not _HAVE_OPUS:
            return None
        try:
            if self._opus_dec is None:
                self._opus_dec = opuslib.Decoder(
                    cfg.AUDIO_RATE, cfg.AUDIO_CHANNELS)
            return self._opus_dec.decode(data, cfg.OPUS_FRAME_SAMPLES)
        except Exception as exc:
            log.error("opus 解码失败: %s", exc)
            return None

    # ---------------- 心跳 ----------------
    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(cfg.HEARTBEAT_INTERVAL_S)
            try:
                self.bridge.ping()
            except Exception:
                pass

    # ---------------- 自动拉起 Py2.7 桥 ----------------
    def _start_bridge_subprocess(self) -> None:
        # ⚠️ 需指定 Py2.7 解释器（pynaoqi 仅 Py2.7）；SDK 环境变量由启动脚本设置
        pepper_dir = os.path.dirname(os.path.abspath(__file__))
        bridge_dir = os.path.join(pepper_dir, "naoqi_bridge")
        bridge_script = os.path.join(bridge_dir, "bridge.py")
        cmd = [self._py2_exe, bridge_script,
               "--qi-url", os.environ.get("PEPPER_QI_URL", cfg.DEFAULT_QI_URL)]
        if self._audio_only:
            cmd.append("--audio-only")
        if self._allow_gestures:
            cmd.append("--allow-gestures")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            (bridge_dir, pepper_dir, env.get("PYTHONPATH", ""))
        )
        try:
            self._bridge_proc = subprocess.Popen(cmd, cwd=bridge_dir, env=env)
            log.info("已拉起 naoqi_bridge 子进程 pid=%s", self._bridge_proc.pid)
        except Exception as exc:
            log.error("拉起 naoqi_bridge 失败: %s", exc)

    # ---------------- 关闭 ----------------
    def shutdown(self) -> None:
        self._stop.set()
        try:
            if self.bridge:
                self.bridge.send_action("stop_all", {}, timeout=3.0)
        except Exception:
            pass
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        if self._bridge_proc:
            try:
                self._bridge_proc.terminate()
            except Exception:
                pass
        log.info("主网关已关闭")


def _rms_int16(pcm: bytes) -> float:
    import array
    if not pcm:
        return 0.0
    samples = array.array("h")
    if hasattr(samples, "frombytes"):
        samples.frombytes(pcm)
    else:
        samples.fromstring(pcm)   # Py2
    if not samples:
        return 0.0
    sumsq = 0
    for s in samples:
        sumsq += s * s
    return (sumsq / len(samples)) ** 0.5


def main() -> None:
    ap = argparse.ArgumentParser(description="Pepper 主网关 suiyue-pepper (Py3)")
    ap.add_argument("--cloud-ws", default=cfg.DEFAULT_CLOUD_WS,
                    help="云端 xiaozhi-esp32-server WS 地址")
    ap.add_argument("--device-id", default=cfg.DEVICE_ID)
    ap.add_argument("--ipc-host", default=cfg.DEFAULT_IPC_HOST)
    ap.add_argument("--ipc-port", type=int, default=cfg.DEFAULT_IPC_PORT)
    ap.add_argument("--audio-only", action="store_true", help="Disable non-whitelisted motion for voice integration")
    ap.add_argument("--allow-gestures", action="store_true", help="Allow only whitelisted expression animations")
    ap.add_argument("--test-text", help="Test cloud reply and robot playback without opening microphone")
    ap.add_argument("--auto-bridge", action="store_true",
                    help="自动拉起 Py2.7 naoqi_bridge 子进程（需 --py2-exe）")
    ap.add_argument("--py2-exe", default=cfg.DEFAULT_PY2_EXE,
                    help="Py2.7 解释器路径（仅 --auto-bridge 时用到）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    gw = PepperGateway(args)

    def _sig(signum, frame):
        log.info("收到信号 %s，关闭中...", signum)
        gw.shutdown()
    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except ValueError:
        pass

    gw.run()


if __name__ == "__main__":
    main()
