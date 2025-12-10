# videoauto

基于 FFmpeg 的视频自动剪辑工具，根据 SRT 字幕时间戳剪辑视频并拼接。

## 功能

- 根据 SRT 字幕时间自动剪辑视频
- 使用 CUDA (h264_nvenc) GPU 加速编码
- 音频响度标准化 (loudnorm)
- 支持 VBR/CBR 编码模式
- 字幕时间同步工具
- 使用 filter_complex_script, 避免超出Windows命令行字符限制

## 安装

```bash
pip install videoauto
```

**依赖**：需要安装 [FFmpeg](https://ffmpeg.org/)（支持 CUDA 的版本）

## 使用

### 视频剪辑

```bash
# 基本用法（自动查找同名 .srt 文件）
videoauto-cutselect video.mp4

# 指定字幕文件
videoauto-cutselect video.mp4 subtitles.srt

# 指定输出文件
videoauto-cutselect video.mp4 -o output.mp4

# 使用 VBR 模式
videoauto-cutselect video.mp4 --vbr --cq 23

# 指定码率
videoauto-cutselect video.mp4 --bitrate 15M
```

### 字幕时间同步

剪辑后的视频时长会变化，使用此工具同步字幕时间：

```bash
videoauto-srt-cutsync video.srt -o video_cut.srt
```

## 原理

1. 解析 SRT 字幕，提取时间片段
2. 合并间隔 < 0.5s 的相邻片段
3. 使用 FFmpeg select 滤镜选择对应帧
4. GPU 编码输出

## select对比trim
本项目提供两种剪辑方案：

### 滤镜组成

| 方案 | 滤镜组成 |
|------|----------|
| select | select + setpts + aselect + asetpts（4 个滤镜） |
| trim | (trim + setpts + atrim + asetpts) × N + 2 个 concat |

### 详细对比

| 对比项 | select/aselect | trim/atrim + concat |
|--------|----------------|---------------------|
| 解码次数 | 1 次 | 1 次 |
| 滤镜复杂度 | 简单（4 个滤镜） | 复杂（每片段 4 个滤镜 + concat） |
| 内存占用 | 较低 | 较高（多个流同时存在） |
| 音视频同步 | 独立选择，可能不同步 | 绑定处理，更精确 |
| 适用场景 | 片段多时更简洁 | 需要精确时间控制 |

说明：因为使用 filter_complex_script, 所以都只需解码1次。

推荐使用 `videoauto-cutselect`（select 方案），片段多时滤镜图更简洁高效。

## License

Apache 2.0