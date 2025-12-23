"""
使用 edge-tts 为 SRT 字幕生成配音。
- 每行字幕用 edge-tts 合成，若合成音频比字幕时长长则自动加速，否则补静音。
- 行间空隙自动补静音。
- 输出音频总时长与 SRT 完全一致。
- 支持自定义 edge-tts 语音。

示例用法：
    python srt_to_voice.py input.srt -o output.wav --voice zh-CN-XiaoxiaoNeural
"""

import asyncio
import edge_tts
import srt
from pydub import AudioSegment, silence
from datetime import timedelta
import argparse
import os
import io
import subprocess
import tempfile

def ffmpeg_speedup(audio: AudioSegment, speed: float) -> AudioSegment:
    """
    pydub.speedup 变速变调，容易破音
    ffmpeg atempo 变速不变调，音质好
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_in, \
         tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_out:
        audio.export(f_in.name, format="wav")
        # atempo 支持 0.5~2.0，超出需多次串联
        atempo_filters = []
        remain = speed
        while remain > 2.0:
            atempo_filters.append("atempo=2.0")
            remain /= 2.0
        while remain < 0.5:
            atempo_filters.append("atempo=0.5")
            remain /= 0.5
        atempo_filters.append(f"atempo={remain:.5f}")
        filter_str = ",".join(atempo_filters)
        cmd = [
            "ffmpeg", "-y", "-i", f_in.name,
            "-filter:a", filter_str,
            f_out.name
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sped = AudioSegment.from_file(f_out.name, format="wav")

    os.unlink(f_in.name)
    os.unlink(f_out.name)
    return sped

def trim_silence(audio: AudioSegment, silence_threshold=-40, chunk_size=10):
    # 返回去除前后静音的音频
    start_trim = silence.detect_leading_silence(audio, silence_threshold=silence_threshold, chunk_size=chunk_size)
    end_trim = silence.detect_leading_silence(audio.reverse(), silence_threshold=silence_threshold, chunk_size=chunk_size)
    duration = len(audio)
    return audio[start_trim:duration-end_trim]

async def synthesize(text, voice="zh-CN-YunjianNeural", rate="+0%", volume="+0%"):
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate, volume=volume)
    mp3_bytes = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_bytes += chunk["data"]
    return AudioSegment.from_file(io.BytesIO(mp3_bytes), format="mp3")

def get_duration_ms(td):
    return int(td.total_seconds() * 1000)

async def srt_to_voice(srt_path, out_path, voice="zh-CN-YunjianNeural"):
    with open(srt_path, encoding="utf-8") as f:
        subs = list(srt.parse(f.read()))

    audio_segments = []
    last_end = timedelta(seconds=0)

    for idx, sub in enumerate(subs, 1):
        print(f"\r正在合成配音：{idx}/{len(subs)}", end="", flush=True)
        # 1. 静音填充
        if sub.start > last_end:
            gap = sub.start - last_end
            audio_segments.append(AudioSegment.silent(duration=get_duration_ms(gap)))

        # 2. 合成语音
        text = sub.content.replace("\n", " ")
        seg = await synthesize(text, voice=voice)
        target_len = get_duration_ms(sub.end - sub.start)
        print(f" (目标时长: {target_len}ms, 合成时长: {len(seg)}ms)")

        # 3. 加速/减速
        if len(seg) > target_len:
            # 先去除前后静音再加速
            seg = trim_silence(seg)
            seg = ffmpeg_speedup(seg, speed=len(seg)/target_len)
            seg = seg[:target_len]
        else:
            seg = seg + AudioSegment.silent(duration=target_len - len(seg))

        audio_segments.append(seg)
        last_end = sub.end

    # 4. 合并所有片段
    final_audio = sum(audio_segments, AudioSegment.silent(duration=0))
    final_audio.export(out_path, format="wav")
    print(f"已保存配音到: {out_path}")

def main():
    parser = argparse.ArgumentParser(description="用 edge-tts 为 SRT 生成配音，自动加速和静音填充")
    parser.add_argument("srt", help="输入 SRT 文件")
    parser.add_argument("-o", "--output", default=None, help="输出音频文件（默认同名 .wav）")
    parser.add_argument("--voice", default="zh-CN-XiaoxiaoNeural", help="edge-tts 语音名")
    args = parser.parse_args()

    out_path = args.output or os.path.splitext(args.srt)[0] + ".wav"
    asyncio.run(srt_to_voice(args.srt, out_path, voice=args.voice))

if __name__ == "__main__":
    main()