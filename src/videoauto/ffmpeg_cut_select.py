"""
FFmpeg 视频剪辑工具 - 使用 select + aselect 方案

功能需求：
    1. 根据 SRT 字幕文件中的时间片段剪辑视频
    2. 将多个片段拼接成一个完整视频
    3. 使用 CUDA (h264_nvenc) 加速编码
    4. 音频采样率转换为 44100Hz
    5. 音频响度标准化 (loudnorm)

实现方案：
    - 使用 FFmpeg 的 select/aselect 滤镜一次性选择所有需要的帧
    - 相比 trim + concat 方案，select 更容易保持音视频同步
    - CPU：解码 + 滤镜处理（select不支持硬件加速）
    - GPU (nvenc)：编码（占70%处理时间）
    - 使用 filter_complex_script 避免命令行过长
    - 支持 VBR (可变码率，剪辑更耗CPU) 和 CBR (恒定码率) 两种模式

效果：
    - 处理速度约为 autocut 的 2-3 倍（视 GPU 性能而定）
    - 输出视频质量良好，音视频同步
    - VBR 模式文件更小，CBR 模式兼容性更好

关键滤镜说明：
    - select='between(t,0,5)+between(t,10,15)': 选择 0-5s 和 10-15s 的视频帧
    - aselect: 同上，用于音频
    - setpts=N/FRAME_RATE/TB: 重建视频时间戳，保证连续播放
    - asetpts=N/SR/TB: 重建音频时间戳
    - loudnorm=I=-16:TP=-1.5:LRA=11: EBU R128 响度标准化
    - format=yuv420p: 确保输出格式兼容

使用方法：
    python -m videoauto.ffmpeg_cut_select video.mp4 video.srt
    python -m videoauto.ffmpeg_cut_select video.mp4 video.srt -o output.mp4
    python -m videoauto.ffmpeg_cut_select video.mp4 video.srt --vbr --cq 23
    python -m videoauto.ffmpeg_cut_select video.mp4 video.srt --bitrate 15M

关于aselect 不生效的临时规避：
    ffmpeg 8.0 版本中，aslect滤镜忘了active，临时使用master版本。
    https://www.reddit.com/r/ffmpeg/comments/1okysj2/aselect_filter_seems_to_not_work/
    [#20949 - Fixed broken aselect filter - FFmpeg/FFmpeg - FFmpeg Forgejo](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/20949/commits)
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
    使用 FFmpeg select/aselect 滤镜剪辑视频

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

    # 计算原始总时长和剪辑后时长
    total_duration = sum(seg["end"] - seg["start"] for seg in segments)
    logging.info(f"找到 {len(segments)} 个片段，预计输出时长: {total_duration:.1f}s")

    # 获取原始视频帧率
    probe_cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=1",
        input_video
    ]
    probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
    fps = probe_result.stdout.strip()  # 格式如 "60/1" 或 "30000/1001"
    logging.info(f"原始视频帧率: {fps}")

    # 构建 select 表达式
    select_expr = "+".join(
        f"between(t,{seg['start']},{seg['end']})" for seg in segments
    )

    # 构建 filter_complex
    # fps: 将 VFR 转为 CFR，确保 select 按时间戳正确选择帧（使用原始帧率）
    # aresample=44100: 在 loudnorm 后重采样，避免输出 96kHz
    filter_complex = (
        f"[0:v]fps={fps},select='{select_expr}',setpts=N/FRAME_RATE/TB,format=yuv420p[outv];"
        f"[0:a]aselect='{select_expr}',asetpts=N/SR/TB,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=44100[outa]"
    )

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
            "-r", "30", # 输出固定30fps减小文件大小
            "-preset", "p4",
        ]

        # 码率控制模式
        if vbr:
            # VBR 可变码率：文件更小，质量稳定
            cmd.extend(["-rc", "vbr", "-cq", str(cq)])
            logging.info(f"使用 VBR 模式 (cq={cq})")
        else:
            # CBR 恒定码率：兼容性更好
            cmd.extend(["-b:v", bitrate])
            logging.info(f"使用 CBR 模式 (bitrate={bitrate})")

        #  faststart 优化，流媒体友好
        cmd.extend(["-max_muxing_queue_size", "1024"])

        # 音频编码（采样率已在滤镜中处理）
        # 使用flac无损编码替代AAC，避免多次编码损失
        cmd.extend([
            "-c:a", "flac",
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
        format="[ffmpeg_cut_select:%(lineno)d] %(levelname)-6s %(message)s",
        level=logging.INFO
    )

    parser = argparse.ArgumentParser(description="使用 FFmpeg 根据 SRT 字幕剪辑视频")
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