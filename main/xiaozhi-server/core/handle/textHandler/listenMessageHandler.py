import time
import asyncio
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from core.handle.receiveAudioHandle import startToChat
from core.handle.reportHandle import enqueue_asr_report
from core.handle.sendAudioHandle import send_stt_message, send_tts_message
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.utils.util import remove_punctuation_and_length
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__

class ListenTextMessageHandler(TextMessageHandler):
    """Listen消息处理器"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.LISTEN

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        if "mode" in msg_json:
            conn.client_listen_mode = msg_json["mode"]
            conn.logger.bind(tag=TAG).debug(
                f"客户端拾音模式：{conn.client_listen_mode}"
            )
        if msg_json["state"] == "start":
            # 设备从播放模式切回录音模式,清除所有音频状态和缓冲区
            conn.reset_audio_states()
        elif msg_json["state"] == "stop":
            conn.client_voice_stop = True
            if conn.asr.interface_type == InterfaceType.STREAM:
                # 流式模式下，发送结束请求
                asyncio.create_task(conn.asr._send_stop_request())
            else:
                # 非流式模式：直接触发ASR识别
                if len(conn.asr_audio) > 0:
                    asr_audio_task = conn.asr_audio.copy()
                    conn.reset_audio_states()

                    if len(asr_audio_task) > 0:
                        await conn.asr.handle_voice_stop(conn, asr_audio_task)
        elif msg_json["state"] == "detect":
            conn.client_have_voice = False

            # 每轮唤醒重置对话主人，重新识别
            conn.conversation_owner = None

            # 尝试用唤醒词音频做声纹识别（低阈值，短音频也能用）
            if conn.voiceprint_provider and conn.voiceprint_provider.enabled and len(conn.asr_audio) > 0:
                wake_word_audio = conn.asr_audio.copy()
                try:
                    pcm_data = conn.asr.decode_opus(wake_word_audio)
                    combined_pcm = b"".join(pcm_data)
                    if len(combined_pcm) > 0:
                        wav_data = conn.asr._pcm_to_wav(combined_pcm)
                        result = await conn.voiceprint_provider.identify_speaker(
                            wav_data, conn.session_id,
                            threshold=conn.voiceprint_provider.wake_word_threshold
                        )
                        if result and result != "未知说话人":
                            conn.conversation_owner = result
                            conn.current_speaker = result
                            conn.logger.bind(tag=TAG).info(f"唤醒词声纹锁定主人: {result}")
                except Exception as e:
                    conn.logger.bind(tag=TAG).warning(f"唤醒词声纹识别失败: {e}")

            conn.reset_audio_states()
            if "text" in msg_json:
                conn.last_activity_time = time.time() * 1000
                original_text = msg_json["text"]
                filtered_len, filtered_text = remove_punctuation_and_length(
                    original_text
                )

                is_wakeup_words = filtered_text in conn.config.get("wakeup_words")
                enable_greeting = conn.config.get("enable_greeting", True)

                if is_wakeup_words and not enable_greeting:
                    await send_stt_message(conn, original_text)
                    await send_tts_message(conn, "stop", None)
                    conn.client_is_speaking = False
                elif is_wakeup_words:
                    conn.just_woken_up = True
                    owner = getattr(conn, "conversation_owner", None)
                    greeting = f"{owner}，我在" if owner else "我在"
                    enqueue_asr_report(conn, greeting, [])
                    await startToChat(conn, greeting)
                else:
                    conn.just_woken_up = True
                    enqueue_asr_report(conn, original_text, [])
                    await startToChat(conn, original_text)