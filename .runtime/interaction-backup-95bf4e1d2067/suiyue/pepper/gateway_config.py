# -*- coding: utf-8 -*-
"""Pepper 主网关（suiyue-pepper）配置常量，Python 3.13 运行。

与 naoqi_bridge/config.py 分离：本文件是主网关侧（Py3）配置；
naoqi_bridge/config.py 是桥侧（Py2.7）配置，两者经本地 IPC 协作。
"""
from __future__ import annotations
import os

# ---- 云端 xiaozhi-esp32-server WS（与 Otto/Reachy 同协议）----
# ⚠️ 端点路径 /xiaozhi/v1 依所部署 server 版本；若 server 用 /xiaozhi 请改。
DEFAULT_CLOUD_WS = os.environ.get("SUIYUE_CLOUD_WS", "ws://192.168.0.87:8000/xiaozhi/v1")
XIAOZHI_PROTOCOL_VERSION = "1.0.0"

# ---- 本地 naoqi_bridge IPC（127.0.0.1:9560 JSON 行）----
DEFAULT_IPC_HOST = "127.0.0.1"
DEFAULT_IPC_PORT = 9560

# ---- Pepper broker（仅 --auto-bridge 拉起子进程时用到）----
DEFAULT_QI_URL = os.environ.get("PEPPER_QI_URL", "tcp://192.168.0.93:9559")

# ---- 设备 ----
DEVICE_ID = os.environ.get("PEPPER_DEVICE_ID", "pepper-01")

# ---- 音频 ----
AUDIO_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_FRAME_DURATION_MS = 60
# 上行/下行音频编码：
#   raw   = PCM 零依赖（需 xiaozhi-server 支持 raw PCM 接收/下发）
#   opus  = 标准（需 opuslib + 系统 libopus；xiaozhi-server 多数要求 opus）
# ⚠️ 默认 opus（最贴近现有 Otto/Reachy 链路）；若 server 不支持 opus 且支持 raw，
#    设 PEPPER_AUDIO_FORMAT=raw 即可零依赖运行。
AUDIO_FORMAT = os.environ.get("PEPPER_AUDIO_FORMAT", "opus")
# 1 = Pepper 通过 ALTextToSpeech 本机朗读文本，不播放云端 TTS 二进制音频。
LOCAL_TTS = os.environ.get("PEPPER_LOCAL_TTS", "1").lower() not in ("0", "false", "no")
OPUS_APPLICATION = "voip"
OPUS_FRAME_SAMPLES = AUDIO_RATE * AUDIO_FRAME_DURATION_MS // 1000  # 960 @16k/60ms

# ---- 心跳（喂 naoqi_bridge 看门狗，桥侧 WATCHDOG_TIMEOUT=15s）----
HEARTBEAT_INTERVAL_S = 5.0

# ---- VAD（麦克风能量检测，控制 xiaozhi listen start/stop）----
VAD_SILENCE_MS = 800                       # 连续静音超过此值 -> listen stop
VAD_ENERGY_THRESHOLD = 300                 # int16 RMS 阈值（0-32767），低于=静音

# ---- emotion -> Pepper 动作原语（对齐 pepper_v1.yaml emotion_mapping）----
# 云端 LLM 输出 emotion 后，主网关据此映射成 naoqi_bridge 能执行的动作原语。
#
#⚠️ 这 6 个键是**岁悦自己的动作真值**（与 brain/capabilities/pepper_v1.yaml
#   的 emotion_mapping 一致），不是云端 server 的标签集。
EMOTION_TO_ACTION = {
    "happy":     ("emote", {"animation": "Wave"}),
    "excited":   ("emote", {"animation": "Wave"}),
    "soothing":  ("posture", {"name": "Crouch"}),   # 蹲下降低视线高度安抚
    "curious":   ("head_nod", {}),
    "concerned": ("approach", {"distance": 0.3}),    # 走到老人身边停住
    "neutral":   ("posture", {"name": "Stand"}),
}

# ---- 云端 emotion 标签 -> 岁悦 emotion（G3 契约翻译层）----
#
# 【为什么必须有这一层 —— 已于2026-10-06 核实 v0.9.7 源码，零交集，不是"待确认"】
# server侧 `core/utils/textUtils.py:84get_emotion()` 只做一件事：
# 在 LLM 回复文本里**扫描第一个 emoji**，用EMOJI_MAP 翻成标签，
# 然后 `{"type":"llm","text":emoji,"emotion":<tag>}` 下发给端侧。
# EMOJI_MAP 共 **21 个标签**（v0.9.7 textUtils.py:8-30）：
#   funny/crying/angry/sad/loving/surprised/shocked/thinking/relaxed/sleepy/
#   silly/confused/neutral/happy/laughing/embarrassed/winking/cool/delicious/
#   kissy/confident
#
# 而上面的 EMOTION_TO_ACTION 只有 6 个键，且与那 21 个**完全不相交**：
#   `excited` / `soothing` / `curious` / `concerned` 在 EMOJI_MAP 里不存在；
#   21 个标签反过来也都不在 6 键里（`neutral`/`happy` 是唯一交集）。
# 后果：`gateway.py:250_emote_from_emotion()` 查表未命中 ->
#   静默回落到 `("posture", {"name": "Stand"})`（L351-352）——
#   **不报错，表现为 Pepper 永远站着不动**（只有 neutral 会碰巧正确）。
#
# 所以这里做**显式翻译**：云端 21 标签 -> 岁悦 6 emotion。未列出的标签
# 走 UNKNOWN_EMOTION_FALLBACK，并且 gateway 会打warning（不再静默）。
SERVER_EMOTION_TO_SUYUE = {
    # 正面 / 欢快
    "happy":      "happy",
    "laughing":   "excited",   # 😆 大笑 -> 挥手机无半步（最接近"兴奋"）
    "funny":      "excited",   # 😂 搞笑
    "silly":      "excited",
    "winking":    "excited",
    "kissy":      "excited",
    "loving":     "happy",
    "delicious":  "happy",
    "confident":  "happy",
    "cool":       "happy",
    "shocked":    "concerned",   # 震惊 -> 关注/靠近
    "surprised":  "concerned",
    # 负面 / 关怀
    "sad":        "soothing",
    "crying":     "soothing",
    "angry":      "concerned",
    "confused":   "curious",
    "thinking":   "curious",
    "relaxed":    "soothing",
    "embarrassed": "soothing",
    "sleepy":     "soothing",
    # 中性
    "neutral":    "neutral",
}

# 云端标签未收录时的兜底动作（保持"不报错但有日志"）。
# 刻意选 neutral/Stand：静默安全姿态，且会打 WARNING 便于发现契约漂移。
UNKNOWN_EMOTION_FALLBACK = "neutral"


def to_suiyue_emotion(server_emotion: str) -> str:
    """云端 emotion 标签 -> 岁悦 emotion（EMOTION_TO_ACTION 的键）。

    未收录的标签回落到 UNKNOWN_EMOTION_FALLBACK。调用方应据返回值是否为
    兜底值来决定是否打warning（gateway.py 已如此实现）。
    """
    return SERVER_EMOTION_TO_SUYUE.get(server_emotion, UNKNOWN_EMOTION_FALLBACK)


# ---- 自动拉起 Py2.7 桥（可选；--auto-bridge 时用到）----
DEFAULT_PY2_EXE = os.environ.get("PYTHON2_EXE", "python2.7")
