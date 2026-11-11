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
        self._tts_playing = threading.Event()   # TTS 播放时暂停上行（打断）
        self._tts_state = "idle"
        self._tts_chunks: list = []
        self._tts_lock = threading.Lock()
        self._cloud_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._auto_bridge = args.auto_bridge
        self._py2_exe = args.py2_exe
        self._bridge_proc: Optional[subprocess.Popen] = None

    # ---------------- 启动 ----------------
    def run(self) -> None:
        if self._auto_bridge:
            self._start_bridge_subprocess()
        self.bridge.connect(retry_timeout=20.0 if self._auto_bridge else 0.0)
        # 心跳线（喂 naoqi_bridge 看门狗）
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        self._connect_cloud()
        log.info("主网关运行中（Ctrl+C 退出）")
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        finally:
            self.shutdown()

    # ---------------- 云端 xiaozhi WS ----------------
    def _connect_cloud(self) -> None:
        if not _HAVE_WS:
            raise RuntimeError("websocket-client 未安装，无法连云端")
        if cfg.AUDIO_FORMAT == "opus" and not _HAVE_OPUS:
            raise RuntimeError("AUDIO_FORMAT=opus 但 Opus 运行库不可用: %s" %
                               _OPUS_IMPORT_ERROR)
        self.ws = websocket.create_connection(self.cloud_ws, enable_multithread=True)
        hello = {
            "type": "hello",
            "version": cfg.XIAOZHI_PROTOCOL_VERSION,
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
                break
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
            ap = msg.get("audio_params") or {}
            log.info("云端 hello 回包 session_id=%s audio_params=%s",
                     self._session_id, ap)
        elif mtype == "stt":
            log.info("[STT] %s", msg.get("text", ""))
        elif mtype == "llm":
            # ⚠️ emotion 字段名依 server/LLM 版本；兼容直接 emotion 或嵌套 llm.emotion
            emotion = msg.get("emotion") or (msg.get("llm") or {}).get("emotion")
            content = msg.get("content") or msg.get("text") or ""
            if emotion:
                self._emote_from_emotion(emotion)
            if content:
                log.info("[LLM] %s", content)
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
                self._tts_playing.set()       # 打断：暂停上行麦克风
            elif state == "sentence_start":
                # ⚠️ 可选：把 text 回显到 Pepper 平板（需主网关托管字幕页，TODO）
                log.info("[TTS] %s", msg.get("text", ""))
            elif state in ("stop", "finish"):
                if self._tts_state == "streaming":
                    self._flush_tts()
                self._tts_state = "idle"
                self._tts_playing.clear()

    def _flush_tts(self) -> None:
        with self._tts_lock:
            chunks = self._tts_chunks
            self._tts_chunks = []
        if not chunks:
            return
        raw = b"".join(chunks)
        pcm = self._decode_audio(raw)
        if pcm is None:
            return
        try:
            self.bridge.send_action("audio_play", {
                "data_b64": base64.b64encode(pcm).decode("ascii"),
                "rate": cfg.AUDIO_RATE,
                "channels": cfg.AUDIO_CHANNELS,
            }, timeout=5.0)
            log.info("[TTS] 已下发 %d 字节音频到 Pepper 扬声器", len(pcm))
        except Exception as exc:
            log.error("TTS 下发失败: %s", exc)

    def _on_cloud_binary(self, data: bytes) -> None:
        with self._tts_lock:
            if self._tts_state == "streaming":
                self._tts_chunks.append(data)

    # ---------------- 上行：Pepper 麦克风 -> 云端 ----------------
    def _on_bridge_audio(self, pcm: bytes, rate: int, channels: int) -> None:
        if self._tts_playing.is_set():
            return   # 打断：TTS 播放时不上行麦克风
        if not self._ensure_listen(pcm):
            return
        frame = self._encode_audio(pcm, rate, channels)
        if frame is None or self.ws is None:
            return
        try:
            self.ws.send_binary(frame)
        except Exception as exc:
            log.error("上行音频失败: %s", exc)

    def _ensure_listen(self, pcm: bytes) -> bool:
        """VAD：有声音 -> listen start 并上行；静音超窗 -> listen stop 并停止上行。"""
        rms = _rms_int16(pcm)
        now = time.time()
        with self._vad_lock:
            if rms > cfg.VAD_ENERGY_THRESHOLD:
                self._silence_since = None
                if not self._listen_active:
                    self._send_listen("start")
                    self._listen_active = True
                return True
            else:
                if self._listen_active:
                    if self._silence_since is None:
                        self._silence_since = now
                    elif now - self._silence_since > cfg.VAD_SILENCE_MS / 1000.0:
                        self._send_listen("stop")
                        self._listen_active = False
                return False

    def _send_listen(self, state: str) -> None:
        if self.ws is None:
            return
        msg = {"type": "listen", "state": state}
        if self._session_id:
            msg["session_id"] = self._session_id
        try:
            self.ws.send(json.dumps(msg))
            log.info("listen %s", state)
        except Exception as exc:
            log.error("listen %s 失败: %s", state, exc)

    # ---------------- emotion -> 动作 ----------------
    def _emote_from_emotion(self, emotion: str) -> None:
        """云端 emotion 标签 -> 岁悦 emotion -> 动作原语。

        G3 契约翻译：云端 server 用的是 `EMOJI_MAP` 的 21 个标签
        （v0.9.7 `core/utils/textUtils.py:8-30`），与本仓
        `EMOTION_TO_ACTION` 的 6 个键**几乎零交集**。必须先经
        `cfg.to_suiyue_emotion()` 翻译，否则永远查表未命中、
        静默回落到Stand（表现为"Pepper 永远站着不动"）。

        未收录标签会打 WARNING —— 契约漂移要能被发现，而不是静默。
        """
        suiyue_emotion = cfg.to_suiyue_emotion(emotion)
        if suiyue_emotion == cfg.UNKNOWN_EMOTION_FALLBACK and \
                emotion != cfg.UNKNOWN_EMOTION_FALLBACK:
            log.warning("云端 emotion 标签未收录于 SERVER_EMOTION_TO_SUYUE: %r "
                        "-> 回落 %s（契约可能漂移，请核对 server 的 EMOJI_MAP）",
                        emotion, cfg.UNKNOWN_EMOTION_FALLBACK)

        action, params = cfg.EMOTION_TO_ACTION.get(
            suiyue_emotion, ("posture", {"name": "Stand"}))
        try:
            self.bridge.send_action(action, params, timeout=5.0)
            log.info("emotion=%s -> 岁悦 %s -> %s %s",
                     emotion, suiyue_emotion, action, params)
        except Exception as exc:
            log.error("emote 失败: %s", exc)

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
