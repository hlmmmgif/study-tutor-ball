# -*- coding: utf-8 -*-
"""
屏幕截图 + PNG 编码 —— 纯标准库实现，不依赖 Pillow。

两个关键实现细节（都踩过坑）：

1. DPI 感知必须在抓屏前声明。
   Windows 默认让非 DPI 感知进程看"逻辑分辨率"。高分屏（如 150% 缩放）
   下 2560x1600 的屏幕会被虚拟化成 1707x1067，抓出来的图被降采样，
   网页上的小字会糊。声明 Per-Monitor V2 后拿到原生像素。

2. 32bpp DIB 的 alpha 通道不可靠。
   GDI 的 GetDIBits 不保证填充 alpha，若原样当 RGBA 用，生成的图会半透明。
   所以统一丢弃 alpha、按不透明处理。

所有 Win32 句柄都用 wintypes 显式标注类型，否则 64 位下会被截断成 32 位导致崩溃。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
import zlib

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
try:
    shcore = ctypes.windll.shcore
except OSError:                                    # 早于 Win8.1
    shcore = None

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

# 显式声明参数/返回类型，避免 64 位句柄被截断。
#
# 踩坑记录：这里最初把位图/对象句柄写成 wt.HBITMAP / wt.HGDIOBJ，
# 结果 64 位下 GDI 句柄高位为 1 时被当成负数，ctypes 直接抛
# "OverflowError: int too long to convert"，抓屏全线失败。
# Windows 官方签名里这些参数就是 HANDLE（无符号指针），统一用 wt.HANDLE。
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wt.HANDLE
gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
gdi32.SelectObject.restype = wt.HANDLE
gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.BitBlt.restype = wt.BOOL
gdi32.DeleteObject.argtypes = [wt.HANDLE]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteDC.restype = wt.BOOL
# GetDIBits 也必须声明，否则 hbm 会按默认的 32 位有符号整数传参，
# 高位为 1 的句柄会直接抛 OverflowError。
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HANDLE, wt.UINT, wt.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
gdi32.GetDIBits.restype = ctypes.c_int


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


_dpi_done = False


def ensure_dpi_aware() -> str:
    """声明本进程为 DPI 感知，才能拿到屏幕原生分辨率。幂等，可重复调用。"""
    global _dpi_done
    if _dpi_done:
        return "already"
    how = "none"
    try:                                            # Win10 1703+：Per-Monitor V2
        ctx = ctypes.c_void_p(-4)                   # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctx):
            how = "per-monitor-v2"
    except Exception:
        pass
    if how == "none" and shcore is not None:
        try:                                        # Win8.1+
            if shcore.SetProcessDpiAwareness(2) == 0:
                how = "per-monitor"
        except Exception:
            pass
    if how == "none":
        try:                                        # Vista+
            if user32.SetProcessDPIAware():
                how = "system"
        except Exception:
            pass
    _dpi_done = True
    return how


def screen_size() -> tuple[int, int]:
    """主屏分辨率。会自动先声明 DPI 感知，所以返回的是物理像素。"""
    ensure_dpi_aware()
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def dpi_awareness_level() -> int:
    """查询当前 DPI 感知级别：0=不感知 1=系统 2=每显示器。"""
    if shcore is None:
        return -1
    val = ctypes.c_int()
    try:
        shcore.GetProcessDpiAwareness(None, ctypes.byref(val))
        return val.value
    except Exception:
        return -1


def grab_screen(region: tuple[int, int, int, int] | None = None) -> tuple[bytes, int, int]:
    """抓取屏幕。region 为 (left, top, width, height)，None 表示整个主屏。

    返回 (rgb_bytes, width, height)，rgb_bytes 为 RGB 排列、每像素 3 字节。
    """
    ensure_dpi_aware()
    if region is None:
        left, top = 0, 0
        width, height = screen_size()
    else:
        left, top, width, height = region
    if width <= 0 or height <= 0:
        raise ValueError(f"无效的截图区域: {region}")

    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        raise OSError("GetDC 失败")
    hdc_mem = None
    hbm = None
    try:
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbm = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
        if not hdc_mem or not hbm:
            raise OSError("创建兼容 DC / 位图失败")
        gdi32.SelectObject(hdc_mem, hbm)
        if not gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, left, top, SRCCOPY):
            raise OSError("BitBlt 失败")

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height            # 负数 = 自上而下，省一次翻转
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32               # 32bpp 最兼容
        info.bmiHeader.biCompression = BI_RGB

        buf = ctypes.create_string_buffer(width * height * 4)
        got = gdi32.GetDIBits(hdc_mem, hbm, 0, height, buf,
                              ctypes.byref(info), DIB_RGB_COLORS)
        if got == 0:
            raise OSError("GetDIBits 失败")

        # BGRA -> RGB，丢弃不可靠的 alpha
        src = buf.raw
        out = bytearray(width * height * 3)
        out[0::3] = src[2::4]
        out[1::3] = src[1::4]
        out[2::3] = src[0::4]
        return bytes(out), width, height
    finally:
        if hbm:
            gdi32.DeleteObject(hbm)
        if hdc_mem:
            gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


def encode_png(rgb: bytes, width: int, height: int, level: int = 6) -> bytes:
    """把 RGB 字节流编码成 PNG。"""
    expected = width * height * 3
    if len(rgb) != expected:
        raise ValueError(f"像素数据长度不符：期望 {expected}，实际 {len(rgb)}")

    # PNG 每行前要加一个 filter 字节。用 filter 1 (Sub，减去左邻像素)
    # 对截图这类大面积纯色区域压缩效果明显更好。
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        row = rgb[y * stride:(y + 1) * stride]
        raw.append(1)
        raw.append(row[0])
        raw.append(row[1])
        raw.append(row[2])
        for i in range(3, stride):
            raw.append((row[i] - row[i - 3]) & 0xFF)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)   # 8bit 真彩色
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), level))
            + chunk(b"IEND", b""))


def capture_png(region: tuple[int, int, int, int] | None = None) -> tuple[bytes, int, int]:
    """抓屏并直接返回 PNG 字节。返回 (png_bytes, width, height)。"""
    rgb, w, h = grab_screen(region)
    return encode_png(rgb, w, h), w, h


if __name__ == "__main__":
    how = ensure_dpi_aware()
    w, h = screen_size()
    print(f"DPI 声明方式: {how}，查询级别: {dpi_awareness_level()}")
    print(f"屏幕分辨率: {w}x{h}")
    png, pw, ph = capture_png()
    out = f"截图测试_{pw}x{ph}.png"
    with open(out, "wb") as f:
        f.write(png)
    print(f"抓屏成功: {pw}x{ph}, PNG {len(png)/1024:.0f} KB -> {out}")
