"""
为 SRT 字幕每行时间戳头尾加 padding 的工具
因为 stable-ts 生成的时间戳较紧凑，部分句子边界被截断。

用法：
    python -m videoauto.srt_padding input.srt -o output.srt --pad 0.1
    python -m videoauto.srt_padding input.srt --inplace --pad 0.1
"""

import argparse
import srt
from datetime import timedelta
import shutil
import os

def pad_srt(input_file, output_file, pad=0.1, encoding="utf-8"):
    with open(input_file, encoding=encoding) as f:
        subs = list(srt.parse(f.read()))
    n = len(subs)
    for i, sub in enumerate(subs):
        # 头部 padding，不能小于 0，也不能超过上一条的 end
        new_start = max(
            timedelta(seconds=0),
            sub.start - timedelta(seconds=pad),
            subs[i-1].end if i > 0 else timedelta(seconds=0)
        )
        # 尾部 padding，不能超过下一条的 start
        new_end = min(
            sub.end + timedelta(seconds=pad),
            subs[i+1].start if i < n-1 else sub.end + timedelta(hours=1)
        )
        sub.start = new_start
        sub.end = new_end
    with open(output_file, "w", encoding=encoding) as f:
        f.write(srt.compose(subs))

def main():
    parser = argparse.ArgumentParser(description="为 SRT 字幕每行时间戳头尾加 padding")
    parser.add_argument("srt", help="输入 SRT 文件")
    parser.add_argument("-o", "--output", help="输出 SRT 文件（默认添加 _pad 后缀）")
    parser.add_argument("--pad", type=float, default=0.1, help="每行头尾 padding 秒数，默认 0.1")
    parser.add_argument("--encoding", default="utf-8", help="文件编码，默认 utf-8")
    parser.add_argument("--inplace", action="store_true", help="原地修改，自动备份为 .back")
    args = parser.parse_args()

    if args.inplace:
        base, ext = os.path.splitext(args.srt)
        backup_file = base + ".back" + ext
        shutil.copyfile(args.srt, backup_file)
        output = args.srt
        print(f"已备份原文件到: {backup_file}")
    else:
        output = args.output or args.srt.replace(".srt", "_pad.srt")
    pad_srt(args.srt, output, pad=args.pad, encoding=args.encoding)
    print(f"已保存到: {output}")

if __name__ == "__main__":
    main()