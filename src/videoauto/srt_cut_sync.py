"""
SRT 字幕时间同步工具

功能：
    根据 ffmpeg_cut_trim.py / ffmpeg_cut_select.py 的剪切逻辑，
    调整字幕时间戳，使其与剪切后的视频同步。

原理：
    剪切视频时，间隔 >= 0.5s 的片段会被拼接在一起，中间的空白被去掉。
    本工具对字幕做同样的时间调整，去掉被剪掉的时间间隔。
    第一条字幕的开始时间前移至 0，与视频起点对齐。

使用方法：
    python -m videoauto.srt_cut_sync input.srt
    python -m videoauto.srt_cut_sync input.srt -o output.srt
    python -m videoauto.srt_cut_sync input.srt --gap 0.3
"""

import argparse
import logging
import os
from datetime import timedelta
from typing import List

import srt


def sync_srt(
    input_file: str,
    output_file: str = None,
    encoding: str = "utf-8",
    max_gap: float = 0.5,
) -> str:
    """
    同步 SRT 字幕时间，与剪切后的视频对齐

    原理与 ffmpeg_cut_select.py 中的 parse_srt_segments 一致：
    - 第一条字幕的开始时间前移至 0
    - 间隔 < max_gap 的字幕保持相对时间不变（属于同一片段）
    - 间隔 >= max_gap 的字幕，前移整个间隔，使其紧邻上一条字幕

    Args:
        input_file: 输入 SRT 文件路径
        output_file: 输出 SRT 文件路径（默认添加 _c 后缀）
        encoding: 文件编码
        max_gap: 最大允许间隔（秒），与视频剪切时的合并阈值一致

    Returns:
        输出文件路径
    """
    if output_file is None:
        base, ext = os.path.splitext(input_file)
        output_file = f"{base}_c{ext}"

    # 读取并解析 SRT
    with open(input_file, encoding=encoding) as f:
        subs = list(srt.parse(f.read()))

    if not subs:
        logging.warning("没有找到有效的字幕")
        return None

    logging.info(f"原始字幕数量: {len(subs)}")

    # 按开始时间排序
    subs.sort(key=lambda x: x.start)

    # 记录原始结束时间
    original_end = max(sub.end for sub in subs)
    logging.info(f"原始结束时间: {original_end}")

    # 调整时间戳
    synced: List[srt.Subtitle] = []
    
    # 第一条字幕的开始时间作为初始偏移量，使其前移至 0
    first_start = subs[0].start
    total_shift = first_start
    logging.debug(f"第一条字幕开始时间: {first_start}, 初始前移: {total_shift.total_seconds():.2f}s")

    for i, sub in enumerate(subs):
        if i > 0:
            prev = subs[i - 1]
            gap = (sub.start - prev.end).total_seconds()

            # 间隔 >= max_gap 时，前移整个间隔（与视频剪切逻辑一致）
            if gap >= max_gap:
                total_shift += timedelta(seconds=gap)
                logging.debug(f"字幕 {i + 1}: 间隔 {gap:.2f}s >= {max_gap}s, 累计前移 {total_shift.total_seconds():.2f}s")

        # 应用累计前移
        new_start = sub.start - total_shift
        new_end = sub.end - total_shift

        synced.append(srt.Subtitle(
            index=i + 1,
            start=new_start,
            end=new_end,
            content=sub.content.strip()
        ))

    # 计算压缩后总时长
    new_end = max(sub.end for sub in synced)
    saved = (original_end - new_end).total_seconds()

    logging.info(f"同步后字幕数量: {len(synced)}")
    logging.info(f"新结束时间: {new_end}")
    logging.info(f"总共压缩: {saved:.1f}s")

    # 写入文件
    with open(output_file, "w", encoding=encoding) as f:
        f.write(srt.compose(synced))

    logging.info(f"已保存到: {output_file}")
    return output_file


def main():
    """命令行入口函数"""
    logging.basicConfig(
        format="[srt_cut_sync:%(lineno)d] %(levelname)-6s %(message)s",
        level=logging.INFO
    )

    parser = argparse.ArgumentParser(
        description="同步 SRT 字幕时间，使其与 ffmpeg_cut 剪切后的视频对齐"
    )
    parser.add_argument("srt", help="输入 SRT 文件")
    parser.add_argument("-o", "--output", help="输出 SRT 文件（默认添加 _c 后缀）")
    parser.add_argument("--encoding", default="utf-8", help="文件编码（默认 utf-8）")
    parser.add_argument("--gap", type=float, default=0.5,
                        help="最大允许间隔（秒），需与视频剪切时的参数一致，默认 0.5")
    parser.add_argument("--inplace", action="store_true", help="直接覆盖原文件")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示详细日志")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    output = args.srt if args.inplace else args.output

    sync_srt(
        args.srt,
        output,
        args.encoding,
        args.gap,
    )


if __name__ == "__main__":
    main()