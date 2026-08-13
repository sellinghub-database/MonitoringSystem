"""Extract Windows file icons for process list (PIL + ImageTk)."""

from __future__ import annotations

import ctypes
import os
from collections import OrderedDict
from typing import Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageTk

# SHGetFileInfo flags
SHGFI_ICON = 0x000000100
SHGFI_SMALLICON = 0x000000001
SHGFI_USEFILEATTRIBUTES = 0x000000010
FILE_ATTRIBUTE_NORMAL = 0x80


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", ctypes.c_void_p),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", ctypes.c_uint32),
        ("szDisplayName", ctypes.c_wchar * 260),
        ("szTypeName", ctypes.c_wchar * 80),
    ]


shell32 = ctypes.windll.shell32
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


def _hicon_to_pil(hicon: int, size: int = 16) -> Optional[Image.Image]:
    if not hicon:
        return None
    hdc = user32.GetDC(0)
    if not hdc:
        return None
    try:
        hbmp = gdi32.CreateCompatibleBitmap(hdc, size, size)
        memdc = gdi32.CreateCompatibleDC(hdc)
        old = gdi32.SelectObject(memdc, hbmp)
        # Fill dark background
        brush = gdi32.CreateSolidBrush(0x0019140F)  # BGR-ish dark
        rect = (ctypes.c_long * 4)(0, 0, size, size)
        user32.FillRect(memdc, ctypes.byref(rect), brush)
        gdi32.DeleteObject(brush)
        user32.DrawIconEx(memdc, 0, 0, hicon, size, size, 0, 0, 0x0003)  # DI_NORMAL

        bmi = BITMAPINFO()
        ctypes.memset(ctypes.byref(bmi), 0, ctypes.sizeof(bmi))
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = size
        bmi.bmiHeader.biHeight = -size
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(size * size * 4)
        gdi32.GetDIBits(memdc, hbmp, 0, size, buf, ctypes.byref(bmi), 0)
        gdi32.SelectObject(memdc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(memdc)

        img = Image.frombuffer("RGBA", (size, size), buf, "raw", "BGRA", 0, 1)
        return img
    except Exception:
        return None
    finally:
        user32.ReleaseDC(0, hdc)
        try:
            user32.DestroyIcon(hicon)
        except Exception:
            pass


def _extract_file_icon(path: str, size: int = 16) -> Optional[Image.Image]:
    if not path or not os.path.isfile(path):
        return None
    sfi = SHFILEINFOW()
    flags = SHGFI_ICON | SHGFI_SMALLICON
    res = shell32.SHGetFileInfoW(path, 0, ctypes.byref(sfi), ctypes.sizeof(sfi), flags)
    if not res or not sfi.hIcon:
        return None
    return _hicon_to_pil(int(sfi.hIcon), size)


def _generic_icon(size: int = 16) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([1, 1, size - 2, size - 2], radius=3, fill=(55, 65, 75, 255), outline=(100, 110, 120, 255))
    return img


class IconCache:
    """LRU cache of PhotoImage icons keyed by exe path."""

    def __init__(self, master, max_items: int = 128, size: int = 16) -> None:
        self.master = master
        self.max_items = max_items
        self.size = size
        self._cache: OrderedDict[str, ImageTk.PhotoImage] = OrderedDict()
        self._fallback = ImageTk.PhotoImage(_generic_icon(size), master=master)
        self._refs = [self._fallback]

    def get(self, exe_path: Optional[str]) -> ImageTk.PhotoImage:
        key = (exe_path or "").lower()
        if not key:
            return self._fallback
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        pil = _extract_file_icon(exe_path or "", self.size) or _generic_icon(self.size)
        if pil.size != (self.size, self.size):
            pil = pil.resize((self.size, self.size), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(pil, master=self.master)
        self._cache[key] = photo
        self._refs.append(photo)
        if len(self._cache) > self.max_items:
            self._cache.popitem(last=False)
        return photo
