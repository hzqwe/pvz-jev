"""Win32 访问层（纯 ctypes，只用标准库）。

Agent 需要的所有 Windows 能力都在这里：
  * 进程发现 + 内存读取
  * 窗口发现 + 几何信息
  * 输入注入（后台 PostMessage / 前台 SendInput）
  * 屏幕抓取（对 DirectDraw 全屏也有效）+ BMP 落盘（便于人工/多模态核对）

设计原则：所有读取函数在失败时返回 None 而不是抛异常，让上层可以安全地
"试探" 多种内存布局解释。
"""

from __future__ import annotations

import ctypes
import struct
import time
from ctypes import wintypes as wt
from dataclasses import dataclass

_k = ctypes.WinDLL("kernel32", use_last_error=True)
_u = ctypes.WinDLL("user32", use_last_error=True)
_g = ctypes.WinDLL("gdi32", use_last_error=True)
_p = ctypes.WinDLL("psapi", use_last_error=True)

# ---------------------------------------------------------------- 常量
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

SRCCOPY = 0x00CC0020
PW_CLIENTONLY = 0x1
PW_RENDERFULLCONTENT = 0x2

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_MOVE = 0x0001

DIB_RGB_COLORS = 0

# PvZ 的图像基址（crash.txt 里 EIP 0x0047269B 印证了这一点）
PVZ_IMAGE_BASE = 0x00400000


def set_dpi_aware() -> str:
    """声明 DPI 感知，否则高 DPI 下 GetWindowRect 与 PostMessage 坐标会对不上。"""
    try:
        # Windows 10 1703+
        ctx = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if _u.SetProcessDpiAwarenessContext(ctx):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        if _u.SetProcessDPIAware():
            return "system"
    except Exception:
        pass
    try:
        _shcore = ctypes.WinDLL("shcore")
        if _shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except Exception:
        pass
    return "none"


DPI_MODE = set_dpi_aware()


# ---------------------------------------------------------------- 结构体
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    cls: str
    title: str
    rect: tuple[int, int, int, int]      # 屏幕坐标 left, top, right, bottom
    client_rect: tuple[int, int, int, int]  # 客户区屏幕坐标
    client_size: tuple[int, int]
    visible: bool

    def client_to_screen(self, x: int, y: int) -> tuple[int, int]:
        return self.client_rect[0] + x, self.client_rect[1] + y

    def describe(self) -> str:
        return (
            f"hwnd=0x{self.hwnd:X} pid={self.pid} class={self.cls!r} "
            f"title={self.title!r} client={self.client_size[0]}x{self.client_size[1]} "
            f"client_origin=({self.client_rect[0]},{self.client_rect[1]}) visible={self.visible}"
        )


# ---------------------------------------------------------------- 进程
def list_processes() -> list[tuple[int, str]]:
    """返回 [(pid, exe_name), ...]。"""
    out: list[tuple[int, str]] = []
    snap = _k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), entry.szExeFile))
            ok = _k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k.CloseHandle(snap)
    return out


def find_pid(names: list[str]) -> int | None:
    """按 exe 名（大小写不敏感）找进程，返回 pid。

    ⚠️ 必须按 `names` 的**优先级**返回，不能按系统枚举顺序 ——
    launcher 和游戏本体常常同时在跑，谁先被 EnumProcesses 枚举到是不确定的，
    按枚举顺序取会随机选中 launcher（它的内存里当然没有 LawnApp）。
    """
    procs = list_processes()
    for name in names:
        low = name.lower()
        for pid, pname in procs:
            if pname.lower() == low:
                return pid
    return None


def module_base(pid: int, module_name: str | None = None) -> int | None:
    """取进程内某模块的加载基址；module_name 为 None 时取主模块。"""
    h = _k.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        arr = (wt.HMODULE * 1024)()
        needed = wt.DWORD()
        if not _p.EnumProcessModules(h, ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(needed)):
            return None
        count = needed.value // ctypes.sizeof(wt.HMODULE)
        for i in range(count):
            buf = ctypes.create_unicode_buffer(MAX_PATH)
            _p.GetModuleBaseNameW(h, arr[i], buf, MAX_PATH)
            if module_name is None or buf.value.lower() == module_name.lower():
                info = ctypes.c_void_p()
                if _p.GetModuleInformation(
                    h, arr[i], ctypes.byref(ctypes.c_void_p()), 0
                ):
                    pass
                # 直接用 HMODULE 值作为基址（PE 模块句柄 == 基址）
                return int(arr[i]) if arr[i] else None
        return None
    finally:
        _k.CloseHandle(h)


class ProcessMemory:
    """只读内存访问。读失败返回 None。"""

    def __init__(self, pid: int, name: str = "", base: int | None = None):
        self.pid = pid
        self.name = name
        self.handle = _k.OpenProcess(
            PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid
        )
        if not self.handle:
            err = ctypes.get_last_error()
            raise OSError(
                f"OpenProcess(pid={pid}) 失败, GetLastError={err}. "
                "若游戏以管理员身份运行，请同样以管理员身份启动本进程。"
            )
        self.base = base if base is not None else (module_base(pid) or PVZ_IMAGE_BASE)
        self._closed = False

    def close(self) -> None:
        if not self._closed and self.handle:
            _k.CloseHandle(self.handle)
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def va(self, addr: int) -> int:
        """把 pvztoolkit 风格的绝对 VA 换算成当前实际地址。"""
        return self.base + (addr - PVZ_IMAGE_BASE)

    def read(self, addr: int, size: int) -> bytes | None:
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = _k.ReadProcessMemory(
            self.handle,
            ctypes.c_void_p(addr),
            buf,
            ctypes.c_size_t(size),
            ctypes.byref(got),
        )
        if not ok or got.value != size:
            return None
        return buf.raw[:size]

    def u8(self, addr: int) -> int | None:
        b = self.read(addr, 1)
        return b[0] if b else None

    def u16(self, addr: int) -> int | None:
        b = self.read(addr, 2)
        return struct.unpack("<H", b)[0] if b else None

    def u32(self, addr: int) -> int | None:
        b = self.read(addr, 4)
        return struct.unpack("<I", b)[0] if b else None

    def i32(self, addr: int) -> int | None:
        b = self.read(addr, 4)
        return struct.unpack("<i", b)[0] if b else None

    def f32(self, addr: int) -> float | None:
        b = self.read(addr, 4)
        return struct.unpack("<f", b)[0] if b else None

    def ptr(self, addr: int) -> int | None:
        return self.u32(addr)


# ---------------------------------------------------------------- 窗口
_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def list_windows(pid: int | None = None) -> list[WindowInfo]:
    """枚举顶层窗口；给 pid 则只返回属于该进程的窗口。"""
    found: list[WindowInfo] = []

    def _cb(hwnd, _lparam):
        wpid = wt.DWORD()
        _u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if pid is not None and wpid.value != pid:
            return True
        found.append(_make_window_info(hwnd, wpid.value))
        return True

    _u.EnumWindows(_WNDENUMPROC(_cb), 0)
    return [w for w in found if w is not None]


def _make_window_info(hwnd: int, pid: int) -> WindowInfo | None:
    cls = ctypes.create_unicode_buffer(256)
    _u.GetClassNameW(hwnd, cls, 256)
    title = ctypes.create_unicode_buffer(512)
    _u.GetWindowTextW(hwnd, title, 512)

    r = wt.RECT()
    if not _u.GetWindowRect(hwnd, ctypes.byref(r)):
        return None

    cr = wt.RECT()
    _u.GetClientRect(hwnd, ctypes.byref(cr))
    pt = wt.POINT(0, 0)
    _u.ClientToScreen(hwnd, ctypes.byref(pt))

    return WindowInfo(
        hwnd=int(hwnd),
        pid=pid,
        cls=cls.value,
        title=title.value,
        rect=(r.left, r.top, r.right, r.bottom),
        client_rect=(pt.x, pt.y, pt.x + cr.right, pt.y + cr.bottom),
        client_size=(cr.right, cr.bottom),
        visible=bool(_u.IsWindowVisible(hwnd)),
    )


def find_game_window(pid: int | None = None, prefer_classes=("MainWindow", "TMainWindow")):
    """找游戏主窗口：优先已知类名，其次取该进程最大且可见的窗口。"""
    wins = list_windows(pid)
    if not wins:
        return None
    for w in wins:
        if w.cls in prefer_classes and w.visible:
            return w
    cands = [w for w in wins if w.visible and w.client_size[0] > 100]
    if not cands:
        cands = wins
    cands.sort(key=lambda w: w.client_size[0] * w.client_size[1], reverse=True)
    return cands[0]


# ---------------------------------------------------------------- 输入
def post_click(hwnd: int, x: int, y: int, settle: float = 0.05) -> None:
    """后台点击：向窗口投递鼠标消息（客户区坐标），不需要窗口获得焦点。"""
    lp = (y & 0xFFFF) << 16 | (x & 0xFFFF)
    _u.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.01)
    _u.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(settle)
    _u.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)


def post_move(hwnd: int, x: int, y: int) -> None:
    lp = (y & 0xFFFF) << 16 | (x & 0xFFFF)
    _u.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)


def post_rclick(hwnd: int, x: int, y: int, settle: float = 0.05) -> None:
    """后台右键。PvZ 里右键 = 取消"手持种子"。

    为什么需要：点种子卡的一瞬间游戏就扣了阳光，如果随后落点失败，种子会一直
    举在手上。而收集阳光的点击正好落在草坪上 —— 下一次收阳光就会把这颗种子
    随便种到某个阳光的位置（而且不花额外阳光，最难察觉）。所以落点失败后必须
    显式取消。
    """
    lp = (y & 0xFFFF) << 16 | (x & 0xFFFF)
    _u.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.01)
    _u.PostMessageW(hwnd, WM_RBUTTONDOWN, MK_RBUTTON, lp)
    time.sleep(settle)
    _u.PostMessageW(hwnd, WM_RBUTTONUP, 0, lp)


SW_RESTORE = 9


def restore_window(hwnd: int) -> bool:
    """把最小化的窗口恢复出来。

    PvZ 一旦被最小化就会**暂停**，同时客户区变成 0x0、窗口坐标变成 -32000，
    此时既读不到有效的对局状态也算不出点击坐标 —— 所以任何操作前先调它。
    """
    if _u.IsIconic(hwnd):
        _u.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.35)
    return not bool(_u.IsIconic(hwnd))


def focus_window(hwnd: int) -> bool:
    try:
        _u.ShowWindow(hwnd, 9)  # SW_RESTORE
        return bool(_u.SetForegroundWindow(hwnd))
    except Exception:
        return False


def is_foreground(hwnd: int) -> bool:
    """窗口是不是当前的**前台窗口**。"""
    try:
        return _u.GetForegroundWindow() == hwnd
    except Exception:
        return False


SW_MINIMIZE = 6


def cycle_window(hwnd: int) -> bool:
    """最小化再恢复，制造一次**真实的激活转换**。

    ⚠️ 这个函数是被一种很隐蔽的故障逼出来的：PvZ 失去焦点后会**停止更新**
    （`game_clock` 冻住），但杂交版**不一定显示暂停菜单** —— 实测抓屏看到的
    就是正常的草坪，`find_pause_resume` 一个绿色按钮都找不到。
    这种"静默暂停"下 `SetForegroundWindow` 也没用：窗口**本来就已经是前台窗口**，
    调用是个 no-op，**不会产生 WM_ACTIVATE**，游戏内部的 mActive 就一直停在
    false。表现极具迷惑性：窗口在最前面、画面正常、进程活着，就是时钟不动、
    点卡没反应。
    minimize -> restore 会走完整的激活流程，能把它拽回来。
    """
    try:
        _u.ShowWindow(hwnd, SW_MINIMIZE)
        time.sleep(0.35)
        _u.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.45)
        _u.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        return not bool(_u.IsIconic(hwnd))
    except Exception:
        return False


def screen_size() -> tuple[int, int]:
    return _u.GetSystemMetrics(0), _u.GetSystemMetrics(1)


def sendinput_click(screen_x: int, screen_y: int, settle: float = 0.04) -> None:
    """前台点击：真实移动光标并产生鼠标事件。全屏 DirectDraw 下更可靠。"""
    _u.SetCursorPos(int(screen_x), int(screen_y))
    time.sleep(0.01)
    _u.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(settle)
    _u.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


# ---------------------------------------------------------------- 抓屏
class Screen:
    """一次抓取，之后可以反复取像素。像素以 BGRA 排列（top-down）。"""

    __slots__ = ("x", "y", "w", "h", "pixels")

    def __init__(self, x: int, y: int, w: int, h: int, pixels: bytes):
        self.x, self.y, self.w, self.h, self.pixels = x, y, w, h, pixels

    def pixel(self, px: int, py: int) -> tuple[int, int, int]:
        """返回 (R, G, B)。"""
        i = (py * self.w + px) * 4
        b, g, r = self.pixels[i], self.pixels[i + 1], self.pixels[i + 2]
        return r, g, b

    def save_png(self, path: str) -> str:
        """写 PNG（标准库 zlib 手写，不引 Pillow），便于多模态模型直接看图。"""
        import zlib

        row_raw = self.w * 4
        raw = bytearray()
        for y in range(self.h):
            raw.append(0)  # filter type: None
            row = self.pixels[y * row_raw : (y + 1) * row_raw]
            for x in range(self.w):
                i = x * 4
                raw += bytes((row[i + 2], row[i + 1], row[i]))  # BGRA -> RGB

        def _chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        blob = (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b"")
        )
        with open(path, "wb") as fh:
            fh.write(blob)
        return path

    def save_bmp(self, path: str) -> str:
        """写 24 位 BMP（自底向上），方便直接用图像查看器 / 多模态模型检查。"""
        row_raw = self.w * 4
        row_out = (self.w * 3 + 3) & ~3
        pad = b"\x00" * (row_out - self.w * 3)
        rows = []
        for y in range(self.h - 1, -1, -1):
            src = self.pixels[y * row_raw : (y + 1) * row_raw]
            line = bytearray()
            for x in range(self.w):
                i = x * 4
                line += bytes((src[i], src[i + 1], src[i + 2]))
            rows.append(bytes(line) + pad)
        body = b"".join(rows)

        header = struct.pack("<2sIHHI", b"BM", 14 + 40 + len(body), 0, 0, 14 + 40)
        info = struct.pack(
            "<IiiHHIIiiII", 40, self.w, self.h, 1, 24, 0, len(body), 2835, 2835, 0, 0
        )
        with open(path, "wb") as fh:
            fh.write(header + info + body)
        return path


def capture_screen(x: int, y: int, w: int, h: int) -> Screen | None:
    """从屏幕 DC 抓一块区域。DirectDraw 全屏也适用（抓的是最终显示内容）。"""
    if w <= 0 or h <= 0:
        return None
    hdc_screen = _u.GetDC(0)
    if not hdc_screen:
        return None
    hdc_mem = _g.CreateCompatibleDC(hdc_screen)
    hbm = _g.CreateCompatibleBitmap(hdc_screen, w, h)
    old = _g.SelectObject(hdc_mem, hbm)
    try:
        if not _g.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, SRCCOPY):
            return None
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # 负数 => top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        got = _g.GetDIBits(hdc_mem, hbm, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        if got == 0:
            return None
        return Screen(x, y, w, h, buf.raw[: w * h * 4])
    finally:
        _g.SelectObject(hdc_mem, old)
        _g.DeleteObject(hbm)
        _g.DeleteDC(hdc_mem)
        _u.ReleaseDC(0, hdc_screen)


def capture_window(win: WindowInfo, use_printwindow: bool = False) -> Screen | None:
    """抓窗口内容。默认走屏幕 DC；PrintWindow 在 DDraw 下常常全黑，故不作默认。"""
    if use_printwindow:
        w, h = win.client_size
        hdc_screen = _u.GetDC(win.hwnd)
        hdc_mem = _g.CreateCompatibleDC(hdc_screen)
        hbm = _g.CreateCompatibleBitmap(hdc_screen, w, h)
        old = _g.SelectObject(hdc_mem, hbm)
        try:
            if _u.PrintWindow(win.hwnd, hdc_mem, PW_RENDERFULLCONTENT | PW_CLIENTONLY):
                bmi = BITMAPINFO()
                bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                bmi.bmiHeader.biWidth = w
                bmi.bmiHeader.biHeight = -h
                bmi.bmiHeader.biPlanes = 1
                bmi.bmiHeader.biBitCount = 32
                buf = ctypes.create_string_buffer(w * h * 4)
                if _g.GetDIBits(hdc_mem, hbm, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS):
                    return Screen(win.client_rect[0], win.client_rect[1], w, h, buf.raw[: w * h * 4])
        finally:
            _g.SelectObject(hdc_mem, old)
            _g.DeleteObject(hbm)
            _g.DeleteDC(hdc_mem)
            _u.ReleaseDC(win.hwnd, hdc_screen)
        # 落到屏幕抓取
    cl, ct, cr, cb = win.client_rect
    return capture_screen(cl, ct, cr - cl, cb - ct)
