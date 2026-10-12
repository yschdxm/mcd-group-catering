# 视频与素材产物

这个目录存放**生成出来的**素材,不是源码。视频体积较大,默认不纳入版本库,
但可以用一条命令完整重建(渲染管线与素材脚本都在仓库里)。

## 已生成

| 文件 | 说明 |
|---|---|
| `麦麦团餐统筹官-演示.mp4` | 88 秒演示视频,1920×1080 @30fps,H.264 |
| `cover.png` | B 站投稿封面,1920×1080 |

## 重新生成

```bash
# 0) 只体检环境(需要 ffmpeg 与 playwright)
python tools/make_video.py --check

# 1) 快速预览若干时间点,先确认画面再花时间全渲染
python tools/make_video.py --duration 88 --preview "3,14,24,33,45,57,67"

# 2) 完整渲染
python tools/make_video.py --duration 88 --fps 30 --crf 20 \
  --out "dist/麦麦团餐统筹官-演示.mp4"

# 3) 封面
python tools/make_video.py --duration 88 --preview "0"   # 或直接用下面的方式
```

封面由 `video/cover.html` 渲染而成,等价于用浏览器截一张 1920×1080 的图:

```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    page = b.new_page(viewport={"width": 1920, "height": 1080})
    page.goto("file:///" + r"...\video\cover.html")
    page.screenshot(path="dist/cover.png")
    b.close()
```

## 管线说明

视频**不是幻灯片拼接**,而是**连续动画**:

- `video/demo.html` 里所有动效都是时间 `t`(秒)的**纯函数**,
  页面只暴露一个 `window.__draw(t)` 入口;
- `tools/make_video.py` 用 Playwright 驱动本机 Edge/Chrome,**逐帧**给 `t` 赋值并截图;
- 再由 ffmpeg 合成 MP4,并在编码器之间自动降级(`libx264 → h264_nvenc → … → mpeg4`)。

这样做的好处:任意时刻的画面都可复现、可只重渲染某一段;
画面质量是网页级的,却没有录屏带来的掉帧与音画不同步。

## 需要的环境

| 依赖 | 说明 |
|---|---|
| ffmpeg | 合成 MP4;需在 PATH 中 |
| playwright | `pip install playwright`;浏览器用本机 Edge/Chrome,无需额外下载 |
| 本机 Edge 或 Chrome | Windows 自带 Edge 即可 |

投稿所需的标题、简介、标签、置顶评论见 [`../video/bilibili.md`](../video/bilibili.md)。
