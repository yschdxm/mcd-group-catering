"""麦麦团餐统筹官 · 演示视频生成器

把「连贯动态动画」渲染成 MP4。核心做法与常见幻灯片视频不同:

**动画时间由外部注入** —— 页面里所有运动都是 ``t``(秒)的纯函数,
通过对 ``window.__draw(t)`` 逐帧赋值来截图。这样得到的是:

* **真正连贯**的动画(元素连续位移、缩放、重排,而不是一页页切换);
* **完全确定性** —— 同一 ``t`` 必然渲染出同一帧,可反复重渲染调试;
* 网页级的排版与视觉效果,却没有录屏带来的掉帧与不同步。

管线:``HTML + 确定时间轴 → Playwright(本机 Edge/Chrome)逐帧截图 → ffmpeg 合成``

用法::

    python tools/make_video.py --out dist/麦麦团餐统筹官-demo.mp4
    python tools/make_video.py --fps 30 --duration 72 --keep-frames
    python tools/make_video.py --check          # 只体检环境,不渲染
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(REPO_ROOT, "dist", "mcd-group-catering-demo.mp4")

#: 分辨率与帧率(B 站 1080p 足够,30fps 在这个动效强度下已经顺滑)
WIDTH, HEIGHT = 1920, 1080
DEFAULT_FPS = 30
DEFAULT_DURATION = 74.0

# ---------------------------------------------------------------- 编码器选择
#: 按优先级尝试;不同构建里可用编码器差异很大(消费级 ffmpeg 常缺 libx264)
ENCODER_FALLBACKS = ("libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_mf", "mpeg4")


def _run(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kwargs)


def probe_ffmpeg() -> Dict[str, Any]:
    """检查 ffmpeg 与可用编码器。"""
    path = shutil.which("ffmpeg")
    if not path:
        return {"ok": False, "reason": "PATH 中找不到 ffmpeg"}
    version = _run([path, "-hide_banner", "-version"])
    encoders_raw = _run([path, "-hide_banner", "-encoders"]).stdout or ""
    available = [name for name in ENCODER_FALLBACKS
                 if f" {name} " in encoders_raw]
    return {
        "ok": bool(available),
        "path": path,
        "version": (version.stdout or "").splitlines()[0] if version.stdout else "",
        "available": available,
        "chosen": available[0] if available else "",
    }


def quality_flags(encoder: str, crf: int) -> List[str]:
    """把 CRF 风格的质量值映射到所选编码器真正支持的参数。

    ``-crf`` 只存在于 libx264/libx265;NVENC 用 ``-cq``,QSV 用 ``-global_quality``,
    AMF 用 ``-qp_i/-qp_p``,MF 用 ``-quality``,mpeg4 只能给 ``-q:v``。
    直接照抄 ``-crf`` 会在非 x264 编码器上直接失败。
    """
    if encoder in ("libx264", "libx265", "libvpx-vp9", "libaom-av1", "libsvtav1"):
        return ["-crf", str(crf)]
    if encoder in ("h264_nvenc", "hevc_nvenc"):
        return ["-rc", "vbr", "-cq", str(max(0, min(51, crf)))]
    if encoder in ("h264_qsv", "hevc_qsv"):
        return ["-global_quality", str(max(1, min(51, crf)))]
    if encoder in ("h264_amf", "hevc_amf"):
        return ["-quality", "quality", "-qp_i", str(crf), "-qp_p", str(crf)]
    if encoder in ("h264_mf", "hevc_mf"):
        return ["-rate_control", "quality",
                "-quality", str(max(1, min(100, round(100 - crf * 1.9))))]
    return ["-q:v", str(max(1, min(31, max(2, crf // 2))))]


def build_encode_command(ffmpeg: str, encoder: str, frames_pattern: str,
                         out_path: str, fps: int, crf: int,
                         audio_path: str = "") -> List[str]:
    """构造 ffmpeg 合成命令。"""
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-framerate", str(fps), "-i", frames_pattern,
    ]
    if audio_path:
        cmd += ["-i", audio_path]
    cmd += [
        "-c:v", encoder, *quality_flags(encoder, crf),
        "-pix_fmt", "yuv420p",          # 兼容性:不转 yuv420p 很多播放器打不开
        "-movflags", "+faststart",      # 便于 B 站/网页边下边播
    ]
    if encoder in ("libx264", "libx265"):
        cmd += ["-preset", "medium"]
    if audio_path:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-shortest"]
    else:
        cmd += ["-an"]
    cmd.append(out_path)
    return cmd


# ------------------------------------------------------------------- 渲染帧
def render_frames(html_path: str, frames_dir: str, fps: int, duration: float,
                  *, channel: str = "msedge", width: int = WIDTH,
                  height: int = HEIGHT, quality: int = 92,
                  progress_every: float = 5.0,
                  scratch: bool = True) -> Dict[str, Any]:
    """用 Playwright 逐帧截图。

    ``window.__draw(t)`` 是页面暴露的唯一入口:给定秒数就渲染出该时刻的画面。
    这保证了「渲染」与「播放」互不干扰,也便于只重渲染某一段。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover
        return {"ok": False, "reason": "未安装 playwright;请先 pip install playwright"}

    os.makedirs(frames_dir, exist_ok=True)
    total = int(round(duration * fps))
    started = time.time()
    last_report = 0.0

    launch_kwargs: Dict[str, Any] = {"headless": True}
    # 优先用本机已装的 Edge/Chrome,避免再下载几百 MB 的 Chromium
    for candidate in (channel, "msedge", "chrome", None):
        if candidate is None:
            break
        try:
            with sync_playwright() as probe:
                probe.chromium.launch(channel=candidate, headless=True).close()
            launch_kwargs["channel"] = candidate
            break
        except Exception:  # noqa: BLE001
            continue

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": width, "height": height},
                                device_scale_factor=1)
        if scratch:
            # 直接在 about:blank 上写 DOM,省掉文件协议与相对路径的麻烦。
            # 顺便把模板里的时长占位替换成实际时长,让进度尺与相机轨迹一致。
            with open(html_path, "r", encoding="utf-8") as handle:
                markup = handle.read()
            markup = markup.replace("/*DURATION*/", str(duration))
            page.set_content(markup, wait_until="load")
        else:
            page.goto("file:///" + html_path.replace("\\", "/"), wait_until="load")
        page.wait_for_timeout(300)

        # 等字体就绪,否则中文可能以回退字体渲染,帧间还会跳变
        try:
            page.evaluate("() => document.fonts && document.fonts.ready")
        except Exception:  # noqa: BLE001
            pass

        for index in range(total):
            t = index / fps
            page.evaluate("(t) => window.__draw(t)", t)
            page.screenshot(path=os.path.join(frames_dir, f"f{index:05d}.png"),
                            type="png", animations="disabled",
                            caret="hide", scale="css")
            if progress_every and (time.time() - last_report) >= progress_every:
                last_report = time.time()
                elapsed = time.time() - started
                done = index + 1
                eta = elapsed / done * (total - done) if done else 0
                print(f"    帧 {done}/{total}  ({done / total * 100:4.1f}%)"
                      f"  已用 {elapsed:5.1f}s  预计剩余 {eta:5.1f}s", flush=True)
        browser.close()

    return {"ok": True, "frames": total, "dir": frames_dir,
            "seconds": round(time.time() - started, 1),
            "channel": launch_kwargs.get("channel", "bundled-chromium")}


def compose(frames_pattern: str, out_path: str, fps: int, crf: int,
            audio_path: str = "") -> Dict[str, Any]:
    """调用 ffmpeg 合成,失败时自动降级编码器。"""
    info = probe_ffmpeg()
    if not info["ok"]:
        return {"ok": False, "reason": info.get("reason") or "没有可用编码器"}

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    errors: List[str] = []
    for encoder in info["available"]:
        cmd = build_encode_command(info["path"], encoder, frames_pattern,
                                   out_path, fps, crf, audio_path)
        result = _run(cmd)
        if result.returncode == 0 and os.path.exists(out_path):
            return {"ok": True, "encoder": encoder,
                    "size": os.path.getsize(out_path), "path": out_path}
        errors.append(f"{encoder}: {(result.stderr or '').strip()[:160]}")
    return {"ok": False, "reason": "所有编码器都失败", "errors": errors}


def preview_frames(html_path: str, out_dir: str, marks: List[float],
                   *, channel: str = "msedge", width: int = WIDTH,
                   height: int = HEIGHT, duration: float = DEFAULT_DURATION) -> List[str]:
    """只渲染若干时间点,用于快速检查动效与排版。"""
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    written: List[str] = []
    launch_kwargs: Dict[str, Any] = {"headless": True}
    for candidate in (channel, "msedge", "chrome"):
        try:
            with sync_playwright() as playwright:
                playwright.chromium.launch(channel=candidate, headless=True).close()
            launch_kwargs["channel"] = candidate
            break
        except Exception:  # noqa: BLE001
            continue

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": width, "height": height},
                                device_scale_factor=1)
        with open(html_path, "r", encoding="utf-8") as handle:
            markup = handle.read().replace("/*DURATION*/", str(duration))
        page.set_content(markup, wait_until="load")
        page.wait_for_timeout(300)
        for mark in marks:
            page.evaluate("(t) => window.__draw(t)", mark)
            path = os.path.join(out_dir, f"preview_{mark:06.2f}s.png")
            page.screenshot(path=path, type="png", animations="disabled")
            written.append(path)
        browser.close()
    return written


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="渲染麦麦团餐统筹官演示视频")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出 MP4 路径")
    parser.add_argument("--html", default=os.path.join(REPO_ROOT, "video", "demo.html"))
    parser.add_argument("--frames-dir", default=os.path.join(REPO_ROOT, "dist", "frames"))
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION,
                        help="视频时长(秒);默认 74")
    parser.add_argument("--crf", type=int, default=20, help="质量,越小越清晰(默认 20)")
    parser.add_argument("--audio", default="", help="可选背景音乐/配音文件")
    parser.add_argument("--channel", default="msedge", help="优先使用的浏览器通道")
    parser.add_argument("--keep-frames", action="store_true", help="保留帧序列")
    parser.add_argument("--check", action="store_true", help="只体检环境")
    parser.add_argument("--preview", default="",
                        help="只渲染若干时间点用于检查动效,如 2,11,18,28,41,48,60")
    args = parser.parse_args(argv)

    info = probe_ffmpeg()
    print("🎬 麦麦团餐统筹官 · 演示视频渲染")
    print("=" * 66)
    print(f"  ffmpeg      {info.get('version') or '未找到'}")
    print(f"  可用编码器  {', '.join(info.get('available') or []) or '无'}")

    has_playwright = True
    try:
        import playwright  # noqa: F401
        print("  playwright  已安装")
    except ImportError:
        has_playwright = False
        print("  playwright  未安装(pip install playwright)")

    if not os.path.exists(args.html):
        print(f"  ✘ 找不到页面模板:{args.html}")
        return 2
    print(f"  模板        {args.html}")

    total_frames = int(round(args.duration * args.fps))
    print(f"  输出        {args.out}")
    print(f"  规格        {WIDTH}x{HEIGHT} @ {args.fps}fps · "
          f"{args.duration:g}s · {total_frames} 帧")
    print("=" * 66)

    if args.check:
        ok = info["ok"] and has_playwright
        print("  结果:环境就绪" if ok else "  结果:环境不完整")
        return 0 if ok else 1
    if not has_playwright or not info["ok"]:
        print("  ✘ 环境不完整,无法渲染(用 --check 复查)")
        return 1

    if args.preview:
        marks = [float(x) for x in args.preview.replace(" ", "").split(",") if x]
        out_dir = os.path.join(REPO_ROOT, "dist", "preview")
        print(f"  ▸ 预览渲染 {len(marks)} 个时间点")
        files = preview_frames(args.html, out_dir, marks, channel=args.channel,
                              duration=args.duration)
        for path in files:
            print(f"    ✔ {path}")
        return 0

    if os.path.isdir(args.frames_dir):
        print("  · 清理旧的帧序列")
        shutil.rmtree(args.frames_dir, ignore_errors=True)

    print("  ▸ 逐帧渲染")
    render = render_frames(args.html, args.frames_dir, args.fps, args.duration,
                           channel=args.channel)
    if not render.get("ok"):
        print(f"  ✘ 渲染失败:{render.get('reason')}")
        return 3
    print(f"    ✔ {render['frames']} 帧,用时 {render['seconds']}s"
          f"(浏览器:{render['channel']})")

    print("  ▸ ffmpeg 合成")
    pattern = os.path.join(args.frames_dir, "f%05d.png")
    composed = compose(pattern, args.out, args.fps, args.crf, args.audio)
    if not composed.get("ok"):
        print(f"  ✘ 合成失败:{composed.get('reason')}")
        for line in composed.get("errors", []):
            print(f"      {line}")
        return 4
    size_mb = composed["size"] / 1024 / 1024
    print(f"    ✔ 编码器 {composed['encoder']},{size_mb:.1f} MB")
    print("=" * 66)
    print(f"  成片:{composed['path']}")

    if not args.keep_frames:
        shutil.rmtree(args.frames_dir, ignore_errors=True)
        print("  · 已清理帧序列(加 --keep-frames 可保留)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
