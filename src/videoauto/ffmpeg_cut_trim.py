"""
FFmpeg 视频剪辑工具 - 使用 trim + concat 方案

功能需求：
    1. 根据 SRT 字幕文件中的时间片段剪辑视频
    2. 将多个片段拼接成一个完整视频
    3. 使用 CUDA (h264_nvenc) 加速编码
    4. 音频采样率转换为 44100Hz
    5. 音频响度标准化 (loudnorm)

实现方案：
    - 使用 FFmpeg 的 trim/atrim 滤镜裁剪每个片段，再 concat 拼接
    - 音视频绑定处理，避免 VFR 视频导致的时长不一致问题
    - CPU：解码 + 滤镜处理
    - GPU (nvenc)：编码
    - 使用 filter_complex_script 避免命令行过长
    - 支持 VBR (可变码率) 和 CBR (恒定码率) 两种模式

与 ffmpeg_cut_select_select.py (select 方案) 的区别：
    - select 方案：视频和音频独立选择，VFR 视频可能导致时长不一致
    - trim 方案：每个片段的音视频使用相同时间范围，确保同步

使用方法：
    python -m videoauto.ffmpeg_cut_trim video.mp4
    python -m videoauto.ffmpeg_cut_trim video.mp4 video.srt
    python -m videoauto.ffmpeg_cut_trim video.mp4 -o output.mp4
    python -m videoauto.ffmpeg_cut_trim video.mp4 --vbr --cq 23
"""

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Dict, List

import srt


def parse_srt_segments(srt_file: str, encoding: str = "utf-8") -> List[Dict[str, float]]:
    """解析 SRT 文件，提取时间片段并合并相邻片段"""
    with open(srt_file, encoding=encoding) as f:
        subs = list(srt.parse(f.read()))

    segments = []
    subs.sort(key=lambda x: x.start)

    for x in subs:
        if len(segments) == 0:
            segments.append({
                "start": x.start.total_seconds(),
                "end": x.end.total_seconds()
            })
        else:
            # 合并间隔小于 0.5 秒的片段
            if x.start.total_seconds() - segments[-1]["end"] < 0.5:
                segments[-1]["end"] = x.end.total_seconds()
            else:
                segments.append({
                    "start": x.start.total_seconds(),
                    "end": x.end.total_seconds()
                })

    return segments


def cut_video(
    input_video: str,
    srt_file: str,
    output_video: str = None,
    encoding: str = "utf-8",
    bitrate: str = "10M",
    vbr: bool = False,
    cq: int = 23,
) -> str:
    """
    使用 FFmpeg trim/atrim + concat 滤镜剪辑视频

    Args:
        input_video: 输入视频文件路径
        srt_file: SRT 字幕文件路径
        output_video: 输出视频文件路径（可选）
        encoding: 字幕文件编码
        bitrate: 视频比特率（CBR 模式使用）
        vbr: 是否使用 VBR 可变码率模式
        cq: VBR 模式的质量参数 (0-51)，值越小质量越高

    Returns:
        输出视频文件路径
    """
    if output_video is None:
        base, _ = os.path.splitext(input_video)
        output_video = f"{base}_c.mp4"

    segments = parse_srt_segments(srt_file, encoding)

    if not segments:
        logging.warning("没有找到有效的字幕片段")
        return None

    # 计算预计输出时长
    total_duration = sum(seg["end"] - seg["start"] for seg in segments)
    logging.info(f"找到 {len(segments)} 个片段，预计输出时长: {total_duration:.1f}s")

    # 构建 filter_complex
    # 使用 trim/atrim 分别裁剪每个片段，然后 concat 拼接
    # 关键：每个片段的视频和音频使用相同的 start/end，确保时长一致
    n = len(segments)
    filter_parts = []

    for i, seg in enumerate(segments):
        start = seg["start"]
        end = seg["end"]
        # trim: 裁剪视频，setpts: 重置时间戳从 0 开始
        # atrim: 裁剪音频，asetpts: 重置时间戳从 0 开始
        filter_parts.append(
            f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}]"
        )
        filter_parts.append(
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]"
        )

    # concat 拼接所有片段
    video_inputs = "".join(f"[v{i}]" for i in range(n))
    audio_inputs = "".join(f"[a{i}]" for i in range(n))
    filter_parts.append(
        f"{video_inputs}concat=n={n}:v=1:a=0,format=yuv420p[outv]"
    )
    filter_parts.append(
        f"{audio_inputs}concat=n={n}:v=0:a=1,aresample=44100,loudnorm=I=-16:TP=-1.5:LRA=11[outa]"
    )

    filter_complex = ";".join(filter_parts)

    # 使用临时文件存储 filter_complex，避免命令行过长
    temp_dir = tempfile.mkdtemp()
    filter_script = os.path.join(temp_dir, "filter.txt")

    try:
        with open(filter_script, "w", encoding="utf-8") as f:
            f.write(filter_complex)

        # 构建 FFmpeg 命令
        cmd = [
            "ffmpeg", "-y",
            "-i", input_video,
            "-filter_complex_script", filter_script,
            "-map", "[outv]",
            "-map", "[outa]",
            "-c:v", "h264_nvenc",
            "-preset", "p4",
        ]

        # 码率控制模式
        if vbr:
            cmd.extend(["-rc", "vbr", "-cq", str(cq)])
            logging.info(f"使用 VBR 模式 (cq={cq})")
        else:
            cmd.extend(["-b:v", bitrate])
            logging.info(f"使用 CBR 模式 (bitrate={bitrate})")

        cmd.extend(["-max_muxing_queue_size", "1024"])

        # 音频编码（采样率已在滤镜中处理）
        cmd.extend([
            "-c:a", "aac",
            "-movflags", "+faststart",
            output_video
        ])

        logging.info("开始处理视频...")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            logging.error(f"FFmpeg 错误: {result.stderr}")
            raise RuntimeError(f"FFmpeg 处理失败: {result.stderr}")

        logging.info(f"视频已保存到: {output_video}")
        return output_video

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    """命令行入口函数"""
    import argparse

    logging.basicConfig(
        format="[ffmpeg_cut_trim:%(lineno)d] %(levelname)-6s %(message)s",
        level=logging.INFO
    )

    parser = argparse.ArgumentParser(description="使用 FFmpeg 根据 SRT 字幕剪辑视频 (trim+concat 方案)")
    parser.add_argument("video", help="输入视频文件")
    parser.add_argument("srt", nargs="?", default=None, help="SRT 字幕文件（默认使用与视频同名的 .srt 文件）")
    parser.add_argument("-o", "--output", help="输出视频文件")
    parser.add_argument("--encoding", default="utf-8", help="字幕文件编码")
    parser.add_argument("--bitrate", default="10M", help="CBR 模式的视频比特率")
    parser.add_argument("--vbr", action="store_true", help="使用 VBR 可变码率模式（文件更小）")
    parser.add_argument("--cq", type=int, default=23, help="VBR 模式的质量参数 (0-51)，值越小质量越高，默认 23")

    args = parser.parse_args()

    # 若未指定 srt 文件，使用与视频同名的 .srt 文件
    srt_file = args.srt
    if srt_file is None:
        base, _ = os.path.splitext(args.video)
        srt_file = f"{base}.srt"

    cut_video(
        args.video,
        srt_file,
        args.output,
        args.encoding,
        args.bitrate,
        args.vbr,
        args.cq,
    )


if __name__ == "__main__":
    main()
