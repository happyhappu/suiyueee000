# -*- coding: utf-8 -*-
"""
Pepper 麦克风音频捕获（Python 2.7 运行）。

参照《NAOqi Python SDK 大学培训文档》2-3 节 audio_record.py：经局域网把 Pepper
麦克风录成原始 PCM 传到本地 PC。本模块订阅**前置麦克风**，把 PCM 帧经回调
on_frame(pcm_bytes, rate, channels) 推给 bridge.py -> 主网关（-> 云 FunASR 流式识别）。

实现方式（经典 NAOqi 音频回调）：
    - 独立 ALBroker 连接 Pepper（与 bridge 的 qi.Session 并存）
    - MicrophoneCapture(ALModule) 注册到 broker，ALAudioDevice 调用其 processRemote
    - processRemote 收到的 buffer 为 int16 LE 字节流，抽取前麦通道后回传

⚠️ 仅按 NAOqi 2.5 文档接口形状编写，未经 NAOqi 2.5.10.7 真机验证；
   若回调签名/通道顺序/事件名有出入，仅改本文件即可，不影响其余逻辑。
"""
from __future__ import print_function, absolute_import

import array
import base64

import config as cfg

try:
    from naoqi import ALBroker, ALModule
except ImportError:
    # 校验/无 SDK 环境：占位，便于静态检查；运行时会明确报错
    ALBroker = None
    ALModule = None


_AUDIO_STATE = {
    "broker": None,
    "module": None,
    "running": False,
    "callback": None,
}


def _extract_front_mic(buffer, nb_of_channels, nbr_of_samples):
    """从 int16 LE 缓冲抽取前麦单声道 PCM 字节。

    - nb_of_channels == 1：buffer 已是单声道，原样返回
    - > 1：交织布局，取 FRONT_MIC_CHANNEL（默认 0）通道
    """
    if not buffer:
        return b""
    samples = array.array("h")
    if hasattr(samples, "frombytes"):
        samples.frombytes(buffer)        # Py3
    else:
        samples.fromstring(buffer)        # Py2
    if nb_of_channels <= 1:
        pcm = samples
    else:
        ch = cfg.FRONT_MIC_CHANNEL
        pcm = samples[ch::nb_of_channels]
    if hasattr(pcm, "tobytes"):
        return pcm.tobytes()              # Py3
    return pcm.tostring()                 # Py2


if ALModule is not None:

    class MicrophoneCapture(ALModule):
        """NAOqi 音频订阅模块；processRemote 由 ALAudioDevice 回调。"""

        def __init__(self, name, on_frame):
            ALModule.__init__(self, name)
            self._on_frame = on_frame
            self._audio = self.getProxy("ALAudioDevice")
            # setClientPreferences(name, rate, channels, deinterleave)
            #   channels: 0=全部4麦交织 / 1=前麦单声道（不同版本语义略有差异，
            #   统一抽取 FRONT_MIC_CHANNEL 兜底）
            self._audio.setClientPreferences(
                name, cfg.AUDIO_RATE, cfg.AUDIO_CHANNELS, cfg.AUDIO_DEINTERLEAVE)
            self._audio.subscribe(name)

        def processRemote(self, nbOfChannels, nbrOfSamples, timeStamp, buffer):
            front = _extract_front_mic(buffer, nbOfChannels, nbrOfSamples)
            if front and self._on_frame is not None:
                self._on_frame(front, cfg.AUDIO_RATE, 1)


def start_audio_stream(qi_url, on_frame, sample_rate=cfg.AUDIO_RATE,
                       channels=cfg.AUDIO_CHANNELS,
                       client_name=cfg.AUDIO_CLIENT_NAME):
    """订阅 Pepper 前置麦克风，逐帧回调 PCM。

    on_frame(pcm_bytes, rate, channels) 由调用方（bridge.py）提供。
    返回 True；失败抛 RuntimeError。
    """
    if ALBroker is None or ALModule is None:
        raise RuntimeError("pynaoqi 未安装（需 Py2.7 + pynaoqi SDK）")
    if _AUDIO_STATE["running"]:
        return True
    _AUDIO_STATE["callback"] = on_frame
    # 独立 ALBroker 连 Pepper（与 bridge 的 qi.Session 并存）
    broker = ALBroker(client_name + "_broker", "0.0.0.0", 0, qiUrl=qi_url)
    module = MicrophoneCapture(client_name, on_frame)
    _AUDIO_STATE["broker"] = broker
    _AUDIO_STATE["module"] = module
    _AUDIO_STATE["running"] = True
    print("[audio] 麦克风订阅已启动 (%dHz, 前麦)" % sample_rate)
    return True


def stop_audio_stream():
    """停止麦克风订阅并释放模块。"""
    if not _AUDIO_STATE["running"]:
        return
    module = _AUDIO_STATE["module"]
    if module is not None:
        try:
            audio_dev = module.getProxy("ALAudioDevice")
            audio_dev.unsubscribe(cfg.AUDIO_CLIENT_NAME)
        except Exception as exc:
            print("[audio] unsubscribe 异常(忽略): %s" % exc)
        try:
            module.exit()
        except Exception:
            pass
    _AUDIO_STATE["broker"] = None
    _AUDIO_STATE["module"] = None
    _AUDIO_STATE["running"] = False
    _AUDIO_STATE["callback"] = None
    print("[audio] 麦克风订阅已停止")


# ---- TTS 播放（云端 GPT-SoVITS 音频经 Pepper 扬声器放出）----
import os as _os
import tempfile as _tempfile
import wave as _wave
import io as _io


def pcm_to_wav_bytes(pcm, rate, channels, sample_width=2):
    """把 16bit LE PCM 字节封装成 WAV 文件字节（Pepper ALAudioPlayer 需文件输入）。"""
    if not pcm:
        return b""
    f = _io.BytesIO()
    w = _wave.open(f, "wb")
    try:
        w.setnchannels(channels)
        w.setsampwidth(sample_width)
        w.setframerate(rate)
        w.writeframes(pcm)
    finally:
        w.close()
    return f.getvalue()


def play_audio(pcm, rate, channels, session, sample_width=2):
    """把 PCM 经 ALAudioPlayer.playToSpeaker 从 Pepper 扬声器放出。

    Pepper 无裸 PCM 播放 proxy；封装 WAV 临时文件 -> ALAudioPlayer.playToSpeaker。
    返回 {played:True} 或 {played:False, reason/error}。
    """
    if not pcm:
        return {"played": False, "reason": "empty"}
    wav = pcm_to_wav_bytes(pcm, rate, channels, sample_width)
    try:
        ap = session.service("ALAudioPlayer")
    except Exception as exc:
        return {"played": False, "reason": "ALAudioPlayer 不可用: %s" % exc}
    if ap is None:
        return {"played": False, "reason": "ALAudioPlayer unavailable"}
    fd, path = _tempfile.mkstemp(suffix=".wav", prefix="pepper_tts_")
    try:
        with _os.fdopen(fd, "wb") as fh:
            fh.write(wav)
        # ⚠️ 真机验证方法名：playToSpeaker / playFile / playToFrontLeft
        ap.playToSpeaker(path)   # 从左前+右前扬声器外放
    except Exception as exc:
        return {"played": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        # 播放异步进行，文件暂留（避免播放中被删导致无声）；下次调用前清理旧文件
        pass  # TODO: 用计时器/上次句柄延迟清理临时 wav
    return {"played": True, "path": path}
