import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import webbrowser
from collections.abc import Coroutine

if os.name == "posix":
    import stat

try:
    import winreg
except ImportError:
    winreg = None

import asyncio
import zipfile
from pathlib import Path
from shutil import copytree, rmtree
from timeit import default_timer as timer
from typing import Any, Callable

import aiohttp
import psutil
import vdf

from tc2_launcher import logger
from tc2_launcher.env import (
    get_desktop_environment,
    get_safe_env,
    get_slr3_path,
    get_ssl_context,
    is_steam_running,
    restore_system_env,
)
from tc2_launcher.hardware import (
    INTEL_VENDOR_ID,
    NVIDIA_VENDOR_ID,
    get_dx_info,
    get_vulkan_info,
)
from tc2_launcher.utils import VERSION_STR

TC2_REPO = "mastercomfig/tc2"
LAUNCHER_REPO = "mastercomfig/tc2-launcher"


def _is_exe_dpi_aware_windows(exe_path: Path | None) -> bool:
    sdl_dpi = os.getenv("SDL_WINDOWS_DPI_AWARENESS", "").strip().lower()
    if sdl_dpi in {"system", "permonitor", "permonitorv2"}:
        return True

    layer_tokens: list[str] = []
    if exe_path and winreg:
        exe_str = str(exe_path)
        layers_subkey = (
            r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
        )
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, layers_subkey) as key:
                    val, _ = winreg.QueryValueEx(key, exe_str)
                    if isinstance(val, str):
                        layer_tokens.extend(val.upper().split())
                        break
            except OSError:
                continue

    env_layer = os.getenv("__COMPAT_LAYER", "")
    if env_layer:
        layer_tokens.extend(env_layer.upper().split())

    if "DPIUNAWARE" in layer_tokens:
        return False
    if "HIGHDPIAWARE" in layer_tokens:
        return True
    return False


def _get_windows_resolution(
    exe_path: Path | None = None,
) -> tuple[int, int] | tuple[None, None]:
    import ctypes
    from ctypes import wintypes

    dpi_aware = _is_exe_dpi_aware_windows(exe_path)

    try:
        user32 = ctypes.WinDLL("user32")
    except Exception:
        return None, None

    class DEVMODEW(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName", wintypes.WCHAR * 32),
            ("dmSpecVersion", wintypes.WORD),
            ("dmDriverVersion", wintypes.WORD),
            ("dmSize", wintypes.WORD),
            ("dmDriverExtra", wintypes.WORD),
            ("dmFields", wintypes.DWORD),
            ("dmPositionX", wintypes.LONG),
            ("dmPositionY", wintypes.LONG),
            ("dmDisplayOrientation", wintypes.DWORD),
            ("dmDisplayFixedOutput", wintypes.DWORD),
            ("dmColor", ctypes.c_short),
            ("dmDuplex", ctypes.c_short),
            ("dmYResolution", ctypes.c_short),
            ("dmTTOption", ctypes.c_short),
            ("dmCollate", ctypes.c_short),
            ("dmFormName", wintypes.WCHAR * 32),
            ("dmLogPixels", wintypes.WORD),
            ("dmBitsPerPel", wintypes.DWORD),
            ("dmPelsWidth", wintypes.DWORD),
            ("dmPelsHeight", wintypes.DWORD),
            ("dmDisplayFlags", wintypes.DWORD),
            ("dmDisplayFrequency", wintypes.DWORD),
            ("dmICMMethod", wintypes.DWORD),
            ("dmICMIntent", wintypes.DWORD),
            ("dmMediaType", wintypes.DWORD),
            ("dmDitherType", wintypes.DWORD),
            ("dmReserved1", wintypes.DWORD),
            ("dmReserved2", wintypes.DWORD),
            ("dmPanningWidth", wintypes.DWORD),
            ("dmPanningHeight", wintypes.DWORD),
        ]

    phys_w: int | None = None
    phys_h: int | None = None
    try:
        EnumDisplaySettingsW = user32.EnumDisplaySettingsW
        EnumDisplaySettingsW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(DEVMODEW),
        ]
        EnumDisplaySettingsW.restype = wintypes.BOOL
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if EnumDisplaySettingsW(None, 0xFFFFFFFF, ctypes.byref(dm)):
            if dm.dmPelsWidth > 0 and dm.dmPelsHeight > 0:
                phys_w = int(dm.dmPelsWidth)
                phys_h = int(dm.dmPelsHeight)
    except Exception:
        pass

    set_ctx = None
    if hasattr(user32, "SetThreadDpiAwarenessContext"):
        set_ctx = user32.SetThreadDpiAwarenessContext
        set_ctx.argtypes = [ctypes.c_void_p]
        set_ctx.restype = ctypes.c_void_p

    GetSystemMetrics = user32.GetSystemMetrics
    GetSystemMetrics.argtypes = [ctypes.c_int]
    GetSystemMetrics.restype = ctypes.c_int

    if dpi_aware:
        if phys_w and phys_h:
            return phys_w, phys_h
        if set_ctx is not None:
            prev_ctx = set_ctx(ctypes.c_void_p(-4)) or set_ctx(ctypes.c_void_p(-2))
            if prev_ctx:
                try:
                    w = GetSystemMetrics(0)
                    h = GetSystemMetrics(1)
                    if w > 0 and h > 0:
                        return w, h
                finally:
                    set_ctx(ctypes.c_void_p(prev_ctx))
    else:
        if phys_w and phys_h and set_ctx is not None:
            try:
                shcore = ctypes.WinDLL("shcore")
                MonitorFromWindow = user32.MonitorFromWindow
                MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
                MonitorFromWindow.restype = ctypes.c_void_p
                GetDpiForMonitor = shcore.GetDpiForMonitor
                GetDpiForMonitor.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_uint),
                    ctypes.POINTER(ctypes.c_uint),
                ]
                GetDpiForMonitor.restype = ctypes.c_long

                prev_ctx = set_ctx(ctypes.c_void_p(-4)) or set_ctx(ctypes.c_void_p(-3))
                if prev_ctx:
                    try:
                        # MONITOR_DEFAULTTOPRIMARY = 1, MDT_EFFECTIVE_DPI = 0
                        hmon = MonitorFromWindow(None, 1)
                        if hmon:
                            dpi_x = ctypes.c_uint(0)
                            dpi_y = ctypes.c_uint(0)
                            hr = GetDpiForMonitor(
                                hmon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
                            )
                            if hr == 0 and dpi_x.value > 0 and dpi_y.value > 0:
                                w = (phys_w * 96 + dpi_x.value // 2) // dpi_x.value
                                h = (phys_h * 96 + dpi_y.value // 2) // dpi_y.value
                                if w > 0 and h > 0:
                                    return w, h
                    finally:
                        set_ctx(ctypes.c_void_p(prev_ctx))
            except Exception:
                pass

        if set_ctx is not None:
            try:
                # DPI_AWARENESS_CONTEXT_UNAWARE = -1
                prev_ctx = set_ctx(ctypes.c_void_p(-1))
                if prev_ctx:
                    try:
                        w = GetSystemMetrics(0)
                        h = GetSystemMetrics(1)
                        if w > 0 and h > 0:
                            return w, h
                    finally:
                        set_ctx(ctypes.c_void_p(prev_ctx))
            except Exception:
                pass

    try:
        w = GetSystemMetrics(0)
        h = GetSystemMetrics(1)
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    return None, None


def _parse_xrandr_output(output: str) -> tuple[int, int] | tuple[None, None]:
    import re

    pattern = re.compile(
        r"^\S+\s+connected\s+(primary\s+)?(\d+)x(\d+)([+-]\d+)([+-]\d+)\b"
    )
    origin_res: tuple[int, int] | None = None
    first_res: tuple[int, int] | None = None

    for line in output.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        is_primary = bool(match.group(1))
        w, h = int(match.group(2)), int(match.group(3))
        if w <= 0 or h <= 0:
            continue
        if is_primary:
            return w, h
        x, y = int(match.group(4)), int(match.group(5))
        if x == 0 and y == 0 and origin_res is None:
            origin_res = (w, h)
        if first_res is None:
            first_res = (w, h)

    if origin_res is not None:
        return origin_res
    if first_res is not None:
        return first_res
    return None, None


def _get_xrandr_ctypes_resolution() -> tuple[int, int] | tuple[None, None]:
    if not os.getenv("DISPLAY"):
        return None, None

    import ctypes

    class XRRScreenResources(ctypes.Structure):
        _fields_ = [
            ("timestamp", ctypes.c_ulong),
            ("configTimestamp", ctypes.c_ulong),
            ("ncrtc", ctypes.c_int),
            ("crtcs", ctypes.POINTER(ctypes.c_ulong)),
            ("noutput", ctypes.c_int),
            ("outputs", ctypes.POINTER(ctypes.c_ulong)),
            ("nmode", ctypes.c_int),
            ("modes", ctypes.c_void_p),
        ]

    class XRROutputInfo(ctypes.Structure):
        _fields_ = [
            ("timestamp", ctypes.c_ulong),
            ("crtc", ctypes.c_ulong),
            ("name", ctypes.c_char_p),
            ("nameLen", ctypes.c_int),
            ("mm_width", ctypes.c_ulong),
            ("mm_height", ctypes.c_ulong),
            ("connection", ctypes.c_ushort),
            ("subpixel_order", ctypes.c_ushort),
            ("ncrtc", ctypes.c_int),
            ("crtcs", ctypes.POINTER(ctypes.c_ulong)),
            ("nclone", ctypes.c_int),
            ("clones", ctypes.POINTER(ctypes.c_ulong)),
            ("nmode", ctypes.c_int),
            ("npreferred", ctypes.c_int),
            ("modes", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class XRRCrtcInfo(ctypes.Structure):
        _fields_ = [
            ("timestamp", ctypes.c_ulong),
            ("x", ctypes.c_int),
            ("y", ctypes.c_int),
            ("width", ctypes.c_uint),
            ("height", ctypes.c_uint),
            ("mode", ctypes.c_ulong),
            ("rotation", ctypes.c_ushort),
            ("noutput", ctypes.c_int),
            ("outputs", ctypes.POINTER(ctypes.c_ulong)),
            ("rotations", ctypes.c_ushort),
            ("npossible", ctypes.c_int),
            ("possible", ctypes.POINTER(ctypes.c_ulong)),
        ]

    with restore_system_env():
        try:
            x11 = ctypes.CDLL("libX11.so.6")
            xrandr = ctypes.CDLL("libXrandr.so.2")

            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            x11.XDefaultRootWindow.restype = ctypes.c_ulong
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay.restype = ctypes.c_int

            xrandr.XRRGetScreenResourcesCurrent.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
            ]
            xrandr.XRRGetScreenResourcesCurrent.restype = ctypes.POINTER(
                XRRScreenResources
            )
            xrandr.XRRFreeScreenResources.argtypes = [
                ctypes.POINTER(XRRScreenResources)
            ]
            xrandr.XRRFreeScreenResources.restype = None
            xrandr.XRRGetOutputPrimary.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            xrandr.XRRGetOutputPrimary.restype = ctypes.c_ulong
            xrandr.XRRGetOutputInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(XRRScreenResources),
                ctypes.c_ulong,
            ]
            xrandr.XRRGetOutputInfo.restype = ctypes.POINTER(XRROutputInfo)
            xrandr.XRRFreeOutputInfo.argtypes = [ctypes.POINTER(XRROutputInfo)]
            xrandr.XRRFreeOutputInfo.restype = None
            xrandr.XRRGetCrtcInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(XRRScreenResources),
                ctypes.c_ulong,
            ]
            xrandr.XRRGetCrtcInfo.restype = ctypes.POINTER(XRRCrtcInfo)
            xrandr.XRRFreeCrtcInfo.argtypes = [ctypes.POINTER(XRRCrtcInfo)]
            xrandr.XRRFreeCrtcInfo.restype = None

            dpy = x11.XOpenDisplay(None)
            if not dpy:
                return None, None
            try:
                root = x11.XDefaultRootWindow(dpy)
                res = xrandr.XRRGetScreenResourcesCurrent(dpy, root)
                if not res:
                    return None, None
                try:
                    primary_out = xrandr.XRRGetOutputPrimary(dpy, root)
                    if primary_out:
                        out_info = xrandr.XRRGetOutputInfo(dpy, res, primary_out)
                        if out_info:
                            try:
                                # RR_Connected == 0
                                if (
                                    out_info.contents.connection == 0
                                    and out_info.contents.crtc
                                ):
                                    crtc_info = xrandr.XRRGetCrtcInfo(
                                        dpy, res, out_info.contents.crtc
                                    )
                                    if crtc_info:
                                        try:
                                            w = int(crtc_info.contents.width)
                                            h = int(crtc_info.contents.height)
                                            if w > 0 and h > 0:
                                                return w, h
                                        finally:
                                            xrandr.XRRFreeCrtcInfo(crtc_info)
                            finally:
                                xrandr.XRRFreeOutputInfo(out_info)

                    origin_res: tuple[int, int] | None = None
                    first_res: tuple[int, int] | None = None
                    for i in range(res.contents.ncrtc):
                        crtc_id = res.contents.crtcs[i]
                        if not crtc_id:
                            continue
                        crtc_info = xrandr.XRRGetCrtcInfo(dpy, res, crtc_id)
                        if not crtc_info:
                            continue
                        try:
                            w = int(crtc_info.contents.width)
                            h = int(crtc_info.contents.height)
                            if crtc_info.contents.noutput > 0 and w > 0 and h > 0:
                                if (
                                    crtc_info.contents.x == 0
                                    and crtc_info.contents.y == 0
                                    and origin_res is None
                                ):
                                    origin_res = (w, h)
                                if first_res is None:
                                    first_res = (w, h)
                        finally:
                            xrandr.XRRFreeCrtcInfo(crtc_info)

                    if origin_res is not None:
                        return origin_res
                    if first_res is not None:
                        return first_res
                finally:
                    xrandr.XRRFreeScreenResources(res)
            finally:
                x11.XCloseDisplay(dpy)
        except Exception:
            pass

    return None, None


def _get_x11_resolution() -> tuple[int, int] | tuple[None, None]:
    if not os.getenv("DISPLAY"):
        return None, None

    try:
        output = subprocess.check_output(
            ["xrandr", "--current"],
            stderr=subprocess.DEVNULL,
            env=get_safe_env(),
            timeout=5,
        ).decode(errors="ignore")
        w, h = _parse_xrandr_output(output)
        if w and h:
            return w, h
    except Exception:
        pass

    return _get_xrandr_ctypes_resolution()


def _parse_wayland_info_output(output: str) -> tuple[int, int] | tuple[None, None]:
    import re

    globals_blocks = output.split("interface: '")

    # Prefer zxdg_output_manager_v1 logical dimensions (accounts for fractional/HiDPI scaling & rotation)
    for block in globals_blocks[1:]:
        if not block.startswith("zxdg_output_manager_v1'"):
            continue
        origin_res: tuple[int, int] | None = None
        first_res: tuple[int, int] | None = None
        for xdg_out in block.split("xdg_output_v1")[1:]:
            lw_match = re.search(r"logical_width:\s*(\d+)", xdg_out)
            lh_match = re.search(r"logical_height:\s*(\d+)", xdg_out)
            if not lw_match or not lh_match:
                continue
            w, h = int(lw_match.group(1)), int(lh_match.group(1))
            if w <= 0 or h <= 0:
                continue
            lx_match = re.search(r"logical_x:\s*(-?\d+)", xdg_out)
            ly_match = re.search(r"logical_y:\s*(-?\d+)", xdg_out)
            if (
                lx_match
                and ly_match
                and int(lx_match.group(1)) == 0
                and int(ly_match.group(1)) == 0
                and origin_res is None
            ):
                origin_res = (w, h)
            if first_res is None:
                first_res = (w, h)
        if origin_res is not None:
            return origin_res
        if first_res is not None:
            return first_res

    # Fallback to wl_output current mode divided by scale
    origin_wl: tuple[int, int] | None = None
    first_wl: tuple[int, int] | None = None
    for block in globals_blocks[1:]:
        if not block.startswith("wl_output'"):
            continue
        parts = block.split("mode:")
        header = parts[0]
        scale_match = re.search(r"scale:\s*(\d+)", header)
        scale = max(1, int(scale_match.group(1))) if scale_match else 1
        transform_match = re.search(r"output_transform:\s*([\w-]+)", header)
        transform = transform_match.group(1) if transform_match else "normal"
        pos_match = re.search(r"\bx:\s*(-?\d+),\s*y:\s*(-?\d+)", header)
        is_origin = (
            bool(pos_match)
            and int(pos_match.group(1)) == 0
            and int(pos_match.group(2)) == 0
        )

        for mode in parts[1:]:
            if re.search(r"flags:.*\bcurrent\b", mode):
                w_match = re.search(r"width:\s*(\d+)", mode)
                h_match = re.search(r"height:\s*(\d+)", mode)
                if w_match and h_match:
                    w = int(w_match.group(1))
                    h = int(h_match.group(1))
                    if "90" in transform or "270" in transform:
                        w, h = h, w
                    w = w // scale
                    h = h // scale
                    if w > 0 and h > 0:
                        if is_origin and origin_wl is None:
                            origin_wl = (w, h)
                        if first_wl is None:
                            first_wl = (w, h)
                        break

    if origin_wl is not None:
        return origin_wl
    if first_wl is not None:
        return first_wl
    return None, None


def _get_wayland_resolution() -> tuple[int, int] | tuple[None, None]:
    if not (os.getenv("WAYLAND_DISPLAY") or os.getenv("XDG_SESSION_TYPE") == "wayland"):
        return None, None

    try:
        output = subprocess.check_output(
            ["wayland-info"],
            stderr=subprocess.DEVNULL,
            env=get_safe_env(),
            timeout=5,
        ).decode(errors="ignore")
        return _parse_wayland_info_output(output)
    except Exception:
        return None, None


def get_native_resolution(
    exe_path: Path | None = None,
) -> tuple[int, int] | tuple[None, None]:
    if os.name == "nt":
        return _get_windows_resolution(exe_path)

    sdl_driver = os.getenv("SDL_VIDEODRIVER", "").strip().lower()
    if sdl_driver == "wayland":
        w, h = _get_wayland_resolution()
        if w and h:
            return w, h
        return _get_x11_resolution()

    w, h = _get_x11_resolution()
    if w and h:
        return w, h
    return _get_wayland_resolution()


def _read_game_settings(game_dir: Path) -> dict:
    settings = {}
    if os.name == "nt":
        if winreg:
            try:
                key_path = r"Software\Valve\Source\tc2\Settings"
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                    for i in range(winreg.QueryInfoKey(key)[1]):
                        name, value, _ = winreg.EnumValue(key, i)
                        try:
                            settings[name] = int(value)
                        except (ValueError, TypeError):
                            settings[name] = value
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.error(f"Failed to read registry settings: {e}")
    else:
        vdf_path = game_dir / "tc2" / "videoconfig_linux.cfg"
        if vdf_path.exists():
            try:
                with vdf_path.open("r", encoding="utf-8") as f:
                    data = vdf.load(f)
                    videoconfig = None
                    for section_name, section_val in data.items():
                        if (
                            isinstance(section_name, str)
                            and section_name.lower() == "videoconfig"
                            and isinstance(section_val, dict)
                        ):
                            videoconfig = section_val
                            break
                    if videoconfig is not None:
                        for k, v in videoconfig.items():
                            try:
                                settings[k] = int(v)
                            except (ValueError, TypeError):
                                settings[k] = v
            except Exception as e:
                logger.error(f"Failed to read VDF settings: {e}")
    return settings


def default_dest_dir() -> Path:
    if os.name == "posix":
        env = "XDG_DATA_HOME"
        fallback = Path.home() / ".local" / "share"
    else:
        env = "LOCALAPPDATA"
        fallback = Path.home() / "AppData" / "Local"

    env = os.getenv(env)
    base = None
    if env:
        base = Path(env)
        if not base.is_absolute():
            base = None
    if not base:
        base = fallback

    dest = base / "TC2Launcher"
    if not dest.exists():
        dest.mkdir(parents=True, exist_ok=True)
    return dest


github_api_headers = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2026-03-10",
    "User-Agent": "TC2Launcher",
}


def _create_http_session() -> aiohttp.ClientSession:
    ssl_ctx = get_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx) if ssl_ctx else None
    return aiohttp.ClientSession(
        headers=github_api_headers,
        connector=connector,
        max_line_size=65536,
        max_field_size=65536,
    )


def run_async_thread(task: Coroutine[Any, Any, Any]):
    # TODO: use run_coroutine_threadsafe
    def run_task():
        asyncio.run(task)

    threading.Thread(target=run_task, daemon=True).start()


def _find_asset(release: dict, asset_filter: str) -> tuple[str, str, str | None] | None:
    assets = release.get("assets") or []
    for a in assets:
        if a.get("state", "uploaded") != "uploaded":
            continue
        name = a.get("name", "")
        download_url = a.get("browser_download_url")
        if download_url and asset_filter.lower() in name.lower():
            return name, download_url, a.get("digest")
    return None


async def _search_releases_for_latest(
    url: str,
    prerelease: bool,
    asset_filter: str,
    session: aiohttp.ClientSession,
    max_pages: int = 3,
) -> dict | None:
    next_url: str | None = url
    saw_stable = False
    try:
        for _ in range(max_pages):
            if not next_url:
                break
            async with session.get(
                next_url, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                resp.raise_for_status()
                releases = await resp.json()
                for release in releases:
                    if release.get("draft", False):
                        continue
                    is_prerelease = release.get("prerelease", False)
                    if not is_prerelease:
                        saw_stable = True
                    if not prerelease and is_prerelease:
                        continue
                    if not _find_asset(release, asset_filter):
                        continue
                    return release
                next_link = resp.links.get("next")
                next_url = str(next_link["url"]) if next_link else None

        if not prerelease and not saw_stable and next_url:
            async with session.get(
                f"{url}/latest", timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                resp.raise_for_status()
                release = await resp.json()
                if (
                    not release.get("draft", False)
                    and not release.get("prerelease", False)
                    and _find_asset(release, asset_filter)
                ):
                    return release
    except Exception as e:
        logger.error(f"Failed to get release searching {url}: {e}")
    return None


async def _get_latest_release(
    dest: Path | None,
    repo: str,
    asset_filter: str,
    session: aiohttp.ClientSession,
) -> dict:
    url = f"https://api.github.com/repos/{repo}/releases"
    prerelease = False
    if repo == TC2_REPO:
        if not dest:
            dest = default_dest_dir()

        settings = read_settings(dest)
        branch = settings.get("branch")
        if branch == "prerelease":
            prerelease = True
        elif branch and branch[0].isdigit():
            try:
                async with session.get(
                    f"{url}/tags/{branch}",
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    resp.raise_for_status()
                    release = await resp.json()
                    if _find_asset(release, asset_filter):
                        return release
                    logger.error(
                        f"No asset matching '{asset_filter}' found in release for branch {branch}, falling back to latest."
                    )
            except Exception as e:
                logger.error(
                    f"Failed to get release for branch {branch}, falling back to latest: {e}"
                )

    release = await _search_releases_for_latest(url, prerelease, asset_filter, session)
    return release or {}


async def update_self() -> bool:
    if os.name == "nt":
        asset_filter = ".exe"
    else:
        asset_filter = "-linux"

    try:
        async with _create_http_session() as session:
            release = await _get_latest_release(
                None, LAUNCHER_REPO, asset_filter, session
            )
            tag = release.get("tag_name", "").lstrip("v")
    except Exception as e:
        logger.error(f"Failed to get self-update info: {e}")
        return False

    if not tag:
        logger.error("Could not determine self-update release tag.")
        return False

    if tag == VERSION_STR:
        logger.info(f"On latest version: {VERSION_STR}")
        return False

    logger.info(f"Self-update available: {VERSION_STR} -> {tag}")

    asset = _find_asset(release, asset_filter)
    if not asset:
        logger.error(
            f"No asset matching '{asset_filter}' found in self-update release {tag}."
        )
        return False

    asset_name, download_url, _ = asset

    dest_dir = default_dest_dir()
    download_path = dest_dir / "update" / tag / asset_name
    logger.info("Downloading self-update...")
    try:
        async with _create_http_session() as session:
            await _download(download_url, download_path, session)
    except Exception as e:
        logger.error(f"Failed to download self-update: {e}")
        return False
    logger.info("Self-update download complete.")

    current_path = Path(sys.argv[0]).resolve()
    logger.info("Launching self-update...")
    try:
        filtered_args = [arg for arg in sys.argv[1:] if arg != "--replace"]
        if os.name == "posix":
            download_path.chmod(
                download_path.stat().st_mode
                | stat.S_IEXEC
                | stat.S_IXGRP
                | stat.S_IXOTH
            )
        run_non_blocking(
            [str(download_path), "--replace", str(current_path)] + filtered_args,
            env={"PYINSTALLER_RESET_ENVIRONMENT": "1"},
        )
        return True
    except Exception as e:
        logger.error(f"Failed to launch self-update: {e}")
        return False


def clean_self_update():
    dest_dir = default_dest_dir()
    update_dir = dest_dir / "update"
    if update_dir.exists() and update_dir.is_dir():
        try:
            rmtree(update_dir)
        except Exception:
            pass


def _read_data(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_data(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _state_path(dest: Path | None = None) -> Path:
    if not dest:
        dest = default_dest_dir()
    return dest / "state.json"


def read_state(dest: Path | None = None) -> dict:
    path = _state_path(dest)
    return _read_data(path)


def write_state(dest: Path | None, state: dict) -> None:
    path = _state_path(dest)
    _write_data(path, state)


def _settings_path(dest: Path | None = None) -> Path:
    if not dest:
        dest = default_dest_dir()
    return dest / "settings.json"


def read_settings(dest: Path | None = None) -> dict:
    path = _settings_path(dest)
    return _read_data(path)


def write_settings(dest: Path | None, settings: dict) -> None:
    path = _settings_path(dest)
    _write_data(path, settings)


async def _download(url: str, dest_path: Path, session: aiohttp.ClientSession) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=3600)) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            async for chunk in r.content.iter_chunked(1024 * 1024):
                if chunk:
                    f.write(chunk)


def _verify_digest(path: Path, digest: str) -> bool:
    with open(path, "rb") as f:
        algo, digest = digest.split(":", 1)
        if algo == "sha256":
            file_digest = hashlib.sha256(f.read()).hexdigest()
        else:
            logger.error(f"Unsupported hash algorithm: {algo}")
            return True
    return file_digest == digest


# https://stackoverflow.com/a/54748564
class ZipFileWithPermissions(zipfile.ZipFile):
    """Custom ZipFile class handling file permissions."""

    def _extract_member(self, member, targetpath, pwd):
        if not isinstance(member, zipfile.ZipInfo):
            member = self.getinfo(member)

        targetpath = super()._extract_member(member, targetpath, pwd)  # type: ignore

        attr = member.external_attr >> 16
        if attr != 0:
            os.chmod(targetpath, attr)
        return targetpath


def _extract_zip(zip_path: Path, extract_dir: Path):
    extract_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        with ZipFileWithPermissions(zip_path, "r") as zf:
            zf.extractall(extract_dir)
    else:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)


game_version_callbacks: list[Callable[[str | None, str | None], None]] = []
game_tag: str | None = None
game_digest: str | None = None


def get_game_version() -> tuple[str | None, str | None]:
    return game_tag, game_digest


def _set_game_version(tag: str | None, digest: str | None) -> None:
    global game_tag, game_digest
    game_tag = tag
    game_digest = digest
    for callback in game_version_callbacks:
        callback(tag, digest)


def subscribe_game_version_change(
    callback: Callable[[str | None, str | None], None],
) -> None:
    global game_tag, game_digest
    callback(game_tag, game_digest)
    game_version_callbacks.append(callback)


def _update_dedicated_dependencies(dest: Path):
    if os.name != "posix":
        return
    import shutil

    logger.info("Updating dedicated server dependencies via steamcmd...")
    cmds = [
        [
            "steamcmd",
            "+force_install_dir",
            str(dest / "SteamLinuxRuntime_sniper"),
            "+login",
            "anonymous",
            "+app_update",
            "1628350",
            "+quit",
        ],
        [
            "steamcmd",
            "+force_install_dir",
            str(dest / "tf2"),
            "+login",
            "anonymous",
            "+app_update",
            "232250",
            "+quit",
        ],
    ]
    for cmd in cmds:
        logger.info(f"Running: {' '.join(cmd)}")
        run_blocking(cmd)

    tf2_dir = dest / "tf2"
    tf2ds_link = dest / "tf2ds"
    if not tf2ds_link.exists() and tf2_dir.exists():
        try:
            tf2ds_link.symlink_to(tf2_dir.name)
        except Exception as e:
            logger.error(f"Failed to create tf2ds symlink: {e}")

    sdk64_dir = Path.home() / ".steam" / "sdk64"
    sdk64_dir.mkdir(parents=True, exist_ok=True)
    steamclient_link = sdk64_dir / "steamclient.so"
    if not steamclient_link.exists():
        steamcmd_path = shutil.which("steamcmd")
        if steamcmd_path:
            steamcmd_real = Path(steamcmd_path).resolve()
            possible_paths = [
                Path.home() / ".steam" / "steamcmd" / "linux64" / "steamclient.so",
                Path.home()
                / ".local"
                / "share"
                / "Steam"
                / "steamcmd"
                / "linux64"
                / "steamclient.so",
                steamcmd_real.parent / "linux64" / "steamclient.so",
            ]
            for p in possible_paths:
                if p.exists():
                    try:
                        steamclient_link.symlink_to(p)
                        break
                    except Exception as e:
                        logger.error(f"Failed to create steamclient.so symlink: {e}")


async def update_archive(
    dest: Path | None = None,
    force: bool = False,
    dedicated: bool = False,
) -> int:
    if not dest:
        dest = default_dest_dir()

    exe_path = _get_game_exe(dest, dedicated)
    if exe_path is not None:
        fail_code = 1
    else:
        fail_code = 2

    if os.name == "nt":
        asset_filter = "game-win.zip"
    else:
        asset_filter = "game-linux.zip"

    try:
        async with _create_http_session() as session:
            release = await _get_latest_release(dest, TC2_REPO, asset_filter, session)
            tag = release.get("tag_name")
            if not tag:
                logger.error("Could not determine release tag.")
    except Exception as e:
        logger.error(f"Failed to fetch latest release info: {e}")
        release = {}
        tag = ""

    state = read_state(dest)
    current_tag = state.get("tag")
    current_digest = state.get("digest")

    _set_game_version(current_tag, current_digest)

    if tag:
        asset = _find_asset(release, asset_filter)
        if not asset:
            logger.error(f"No asset matching '{asset_filter}' found in release {tag}.")
            return fail_code
        asset_name, download_url, digest = asset
        if (
            not force
            and current_tag == tag
            and current_digest == digest
            and fail_code == 1
        ):
            logger.info("Latest asset already downloaded.")
            return 0
    else:
        asset_name = asset_filter
        download_url = (
            f"https://github.com/mastercomfig/tc2/releases/latest/download/{asset_name}"
        )
        digest = None

    logger.info(f"Latest release tag: {tag}")
    logger.info(f"Current release tag: {current_tag}")
    logger.info(f"Selected asset: {asset_name}")

    # asset_path = dest / asset_name
    with tempfile.TemporaryDirectory(
        prefix="TC2Launcher", ignore_cleanup_errors=True
    ) as tmp_dir_name:
        asset_path = Path(tmp_dir_name) / asset_name
        game_dir = get_game_dir(dest)

        logger.info(f"Downloading latest asset to {asset_path}...")
        try:
            async with _create_http_session() as session:
                await _download(download_url, asset_path, session)
        except Exception as e:
            logger.error(f"Failed to download asset: {e}")
            return fail_code
        logger.info("Download complete.")

        logger.info(f"Extracting asset to {game_dir}...")
        if asset_path.exists():
            asset_ok = True
            if digest:
                asset_ok = _verify_digest(asset_path, digest)
            if not asset_ok:
                logger.error("Asset verification failed.")
                return fail_code
            elif asset_name.lower().endswith(".zip"):
                _extract_zip(asset_path, game_dir)

    if not game_dir.exists():
        logger.error(f"Game directory '{game_dir}' does not exist after extraction.")
        return fail_code

    exe_path = _get_game_exe(dest, dedicated)
    if not exe_path:
        logger.error("Could not locate game executable after update.")
        return -2

    logger.info("Extraction complete.")

    if dedicated:
        _update_dedicated_dependencies(dest)

    written = False
    if tag:
        state["tag"] = tag
        written = True
    if digest:
        state["digest"] = digest
        written = True
    if written:
        _set_game_version(tag, digest)
        write_state(dest, state)

    return 0


def run_open(url: str):
    if os.name == "nt":
        browser_protocols = ["http://", "https://", "ftp://", "file://"]
        if any(url.startswith(p) for p in browser_protocols):
            webbrowser.open(url)
        else:
            os.startfile(url)
    else:
        with restore_system_env():
            args = []
            desktop_env = get_desktop_environment()
            import shutil

            # this is a bit non-ideal, since it's mostly a copy of webbrowser.open()
            if desktop_env == "kde":
                for cmd in [
                    "kioclient5",
                    "kioclient",
                    "kde-open6",
                    "kde-open5",
                    "kde-open",
                ]:
                    if shutil.which(cmd):
                        if "kioclient" in cmd:
                            args = [cmd, "exec", url]
                        else:
                            args = [cmd, url]
                        break
            elif desktop_env == "gnome":
                for cmd in ["gio", "gvfs-open", "gnome-open"]:
                    if shutil.which(cmd):
                        if cmd == "gio":
                            args = ["gio", "open", "--", url]
                        else:
                            args = [cmd, url]
                        break

            if not args:
                args = ["xdg-open", url]

            run_non_blocking(args)


def run_blocking(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, env=get_safe_env(), cwd=cwd, shell=False)


def run_non_blocking(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict | None = None,
) -> subprocess.Popen:
    new_env = get_safe_env()
    if env:
        new_env.update(env)

    if os.name == "nt":
        creationflags = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        return subprocess.Popen(
            cmd,
            env=new_env,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
    else:
        if isinstance(cmd, list) and cmd[0] != "nohup":
            cmd.insert(0, "nohup")

        cmd = " ".join(cmd) if isinstance(cmd, list) else cmd

        return subprocess.Popen(
            cmd,
            env=new_env,
            cwd=cwd,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )


_game_dir_lock = threading.RLock()
_has_randomized_this_session = False
_logged_defer_migration = False
MIN_INSTALL_PATH_LENGTH = 24


def get_path_length(path: Path) -> int:
    return len(path.as_posix().rstrip("/"))


def is_path_too_short(path: Path) -> bool:
    return get_path_length(path) < MIN_INSTALL_PATH_LENGTH


def _is_legacy_tc2_dir_name(path: Path) -> bool:
    folder_name = path.name.lower()
    return folder_name == "tc2" or folder_name.startswith("tc2_")


def _dir_has_entries(path: Path) -> tuple[bool, bool]:
    try:
        return any(path.iterdir()), False
    except OSError:
        return False, True


def _can_write_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return False
        with tempfile.NamedTemporaryFile(dir=path, prefix=".tc2_write_", delete=True):
            pass
        return True
    except OSError:
        return False


def _is_game_running_in_dir(directory: Path) -> bool:
    game_exe_name = _get_game_exe_name(running_process=True).lower()
    try:
        resolved_dir = directory.resolve()
    except OSError:
        resolved_dir = directory
    for p in psutil.process_iter(["name", "exe"]):
        try:
            name = p.info.get("name")
            if not name or name.lower() != game_exe_name:
                continue
            exe = p.info.get("exe")
            if exe and Path(exe).resolve().is_relative_to(resolved_dir):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return False


def _get_padded_game_dir(path: Path) -> Path:
    if path.name.lower() == "games":
        games_dir = path / "Team Comtress 2"
        if not is_path_too_short(games_dir) and not games_dir.exists():
            return games_dir

    base_dir = path.with_name("Team Comtress 2")
    if not is_path_too_short(base_dir) and not base_dir.exists():
        return base_dir

    import uuid

    base_len = get_path_length(base_dir)
    suffix_len = max(4, MIN_INSTALL_PATH_LENGTH - base_len)
    while True:
        random_suffix = uuid.uuid4().hex[:suffix_len]
        candidate = path.with_name(f"Team Comtress 2 {random_suffix}")
        if not candidate.exists():
            return candidate
        suffix_len = min(32, suffix_len + 1)


def _rename_game_dir(old_path: Path, new_path: Path) -> None:
    old_path = old_path.resolve()
    new_path = new_path.resolve()
    if old_path == new_path:
        return

    if new_path.is_relative_to(old_path):
        import uuid

        temp_path = old_path.parent / f"tc2_migrate_{uuid.uuid4().hex[:8]}"
        while temp_path.exists():
            temp_path = old_path.parent / f"tc2_migrate_{uuid.uuid4().hex[:8]}"
        old_path.rename(temp_path)
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.rename(new_path)
        except Exception:
            try:
                temp_path.rename(old_path)
            except Exception:
                pass
            raise
    else:
        has_entries, unreadable = (
            _dir_has_entries(new_path)
            if new_path.exists() and new_path.is_dir()
            else (False, False)
        )
        if (
            new_path.exists()
            and new_path.is_dir()
            and not has_entries
            and not unreadable
        ):
            new_path.rmdir()
        new_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.rename(new_path)


def _copy_migrate_game_dir(old_path: Path, new_path: Path) -> None:
    old_path = old_path.resolve()
    new_path = new_path.resolve()
    if old_path == new_path:
        return

    if new_path.is_relative_to(old_path):
        import uuid

        temp_path = old_path.parent / f"tc2_migrate_{uuid.uuid4().hex[:8]}"
        while temp_path.exists():
            temp_path = old_path.parent / f"tc2_migrate_{uuid.uuid4().hex[:8]}"
        copytree(old_path, temp_path, dirs_exist_ok=True)
        try:
            for item in list(old_path.iterdir()):
                try:
                    if item.is_dir() and not item.is_symlink():
                        rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
                except OSError:
                    pass
            new_path.mkdir(parents=True, exist_ok=True)
            copytree(temp_path, new_path, dirs_exist_ok=True)
        finally:
            rmtree(temp_path, ignore_errors=True)
    else:
        target_was_empty = not new_path.exists() or not _dir_has_entries(new_path)[0]
        try:
            new_path.mkdir(parents=True, exist_ok=True)
            copytree(old_path, new_path, dirs_exist_ok=True)
        except Exception:
            if target_was_empty:
                rmtree(new_path, ignore_errors=True)
            raise
        rmtree(old_path, ignore_errors=True)


def get_game_dir(dest: Path | None = None) -> Path:
    global _has_randomized_this_session
    global _logged_defer_migration

    with _game_dir_lock:
        default_dest = default_dest_dir()
        default_game_dest = default_dest / "game"
        is_default_dest = not dest or dest == default_dest

        settings = read_settings(dest)
        user_game_dir = settings.get("game_dir")

        if os.name == "nt" and not _has_randomized_this_session:
            is_unset = not user_game_dir
            is_default_tc2 = False
            is_too_short = False
            if user_game_dir:
                try:
                    resolved_user_dir = Path(user_game_dir).resolve()
                    if _is_legacy_tc2_dir_name(resolved_user_dir):
                        is_default_tc2 = True
                    elif is_path_too_short(resolved_user_dir):
                        is_too_short = True
                except Exception:
                    pass

            drive = Path.home().drive
            legacy_root_tc2 = Path(f"{drive}\\") / "tc2"
            should_init_unset = (
                is_unset
                and is_default_dest
                and (legacy_root_tc2.exists() or not default_game_dest.exists())
            )

            if should_init_unset or is_default_tc2 or is_too_short:
                if is_unset:
                    old_path = legacy_root_tc2
                    new_path = Path(f"{drive}\\") / "Games" / "Team Comtress 2"
                else:
                    old_path = Path(user_game_dir).resolve()
                    new_path = old_path.parent / "Team Comtress 2"
                    if is_path_too_short(new_path):
                        new_path = old_path.parent / "Games" / "Team Comtress 2"

                rename_succeeded = False
                deferred = False

                if old_path.exists() and old_path.is_dir() and not old_path.is_mount():
                    old_has_entries, old_unreadable = _dir_has_entries(old_path)
                    if not old_has_entries:
                        if not old_unreadable:
                            try:
                                old_path.rmdir()
                            except OSError:
                                pass
                        if not _can_write_dir(new_path):
                            new_path = default_game_dest
                        rename_succeeded = True
                    elif _is_game_running_in_dir(old_path):
                        if not _logged_defer_migration:
                            logger.info(
                                f"Deferring game directory migration while game is running in {old_path}"
                            )
                            _logged_defer_migration = True
                        deferred = True
                        user_game_dir = str(old_path)
                    else:
                        if (
                            new_path.exists()
                            and not new_path.is_relative_to(old_path)
                            and (not new_path.is_dir() or _dir_has_entries(new_path)[0])
                        ):
                            new_path = _get_padded_game_dir(new_path)
                        try:
                            _rename_game_dir(old_path, new_path)
                            logger.info(
                                f"Renamed default game directory from {old_path} to {new_path}"
                            )
                            rename_succeeded = True
                        except Exception as rename_err:
                            copy_target = new_path
                            if copy_target.is_relative_to(old_path):
                                copy_target = _get_padded_game_dir(old_path)
                            target_existed = (
                                copy_target.exists()
                                and _dir_has_entries(copy_target)[0]
                            )
                            if not _can_write_dir(copy_target):
                                if not target_existed:
                                    rmtree(copy_target, ignore_errors=True)
                                copy_target = default_game_dest
                                if (
                                    copy_target.exists()
                                    and _dir_has_entries(copy_target)[0]
                                ):
                                    copy_target = _get_padded_game_dir(copy_target)
                            try:
                                _copy_migrate_game_dir(old_path, copy_target)
                                new_path = copy_target
                                logger.info(
                                    f"Migrated default game directory from {old_path} to {new_path}"
                                )
                                rename_succeeded = True
                            except Exception as copy_err:
                                logger.error(
                                    f"Failed to rename default game directory from {old_path} to {new_path}: {rename_err} (copy fallback failed: {copy_err})"
                                )
                                if (
                                    not (old_path / _get_game_exe_name()).exists()
                                    and not _can_write_dir(old_path)
                                    and _can_write_dir(copy_target)
                                ):
                                    new_path = copy_target
                                    rename_succeeded = True
                else:
                    if not _can_write_dir(new_path):
                        new_path = default_game_dest
                    rename_succeeded = True

                if rename_succeeded:
                    settings["game_dir"] = str(new_path)
                    try:
                        write_settings(dest, settings)
                    except Exception as e:
                        logger.error(
                            f"Failed to write settings with migrated game directory: {e}"
                        )
                    user_game_dir = str(new_path)
                    _has_randomized_this_session = True
                    _logged_defer_migration = False
                elif not deferred:
                    user_game_dir = str(old_path)
                    _has_randomized_this_session = True
            else:
                _has_randomized_this_session = True

        if user_game_dir:
            try:
                resolved_dest = Path(user_game_dir).resolve()
                resolved_dest.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.error(
                    f"Failed to use specified game directory '{user_game_dir}': {e}"
                )
                resolved_dest = None
            if resolved_dest and resolved_dest.exists() and resolved_dest.is_dir():
                return resolved_dest

        if is_default_dest:
            if os.name == "nt" and not default_game_dest.exists():
                drive = Path.home().drive
                win_default = Path(f"{drive}\\") / "Games" / "Team Comtress 2"
                if _can_write_dir(win_default):
                    return win_default
            default_game_dest.mkdir(parents=True, exist_ok=True)
            return default_game_dest

        custom_game_dest = dest / "game"
        if os.name == "nt" and is_path_too_short(custom_game_dest):
            custom_game_dest = dest / "Team Comtress 2"
            if is_path_too_short(custom_game_dest):
                custom_game_dest = _get_padded_game_dir(custom_game_dest)
        return custom_game_dest


def _get_game_exe_name(running_process: bool = False, dedicated: bool = False) -> str:
    # Determine executable name based on platform
    if os.name == "nt":
        return "tc2_win64.exe"
    else:
        if dedicated and not running_process:
            return "start_dedicated_tc2.sh"
        return "tc2_linux64"


def _get_game_exe(dest: Path | None, dedicated: bool = False) -> Path | None:
    if not dest:
        dest = default_dest_dir()

    game_dir = get_game_dir(dest)

    # Determine executable based on platform
    exe_path = game_dir / _get_game_exe_name(dedicated=dedicated)

    if exe_path and exe_path.exists():
        return exe_path
    return None


def launch_game(
    dest: Path | None = None,
    extra_options: list[str] | None = None,
    dedicated: bool = False,
    url_options: list[str] | None = None,
) -> tuple[str | None, bool, subprocess.Popen | None]:
    if not dest:
        dest = default_dest_dir()

    exe_path = _get_game_exe(dest, dedicated)
    if not exe_path or not exe_path.exists():
        logger.error(f"Could not locate game executable '{exe_path}'")
        return f"Could not locate game executable '{exe_path}'", False, None

    default_args = [
        "-steam",
        "-particles",
        "1",
    ]
    default_cmds = ["+ip", "127.0.0.1", "+sv_pure", "1"]
    if os.name == "posix":
        default_args += ["-gathermod"]

    # Resolve options with persistence
    settings = read_settings(dest)
    if not extra_options:
        extra_options = settings.get("opts")
    if not extra_options or not isinstance(extra_options, list):
        extra_options = []

    if url_options:
        extra_options.extend(url_options)

    if dedicated:
        default_args += ["-console", "-dedicated"]
        if os.name == "posix":
            if not is_steam_running():
                default_args += ["-nosteamclient"]

    vk_supported, vk_gpu_info, vk_error_msg = get_vulkan_info()
    supported = vk_supported
    gpu_info = vk_gpu_info
    error_reason = vk_error_msg
    if not supported and os.name == "nt":
        dx_supported, dx_gpu_info, dx_error_msg = get_dx_info()
        supported = dx_supported
        if dx_supported:
            gpu_info = dx_gpu_info
        else:
            error_reason = dx_error_msg
    if not supported:
        error_text = (
            "Your graphics card falls below our official minimum specs.\n\n"
            "In previous versions, this spec was recommended for minor graphical\n"
            "improvements using these hardware capabilities. However,\n"
            "Team Comtress 2 now relies heavily on graphics features such as \n"
            "Shader Model 3.0 and so adherence to the prior recommended spec\n"
            "is now the minimum requirement.\n\n"
            "Unfortunately this means that Team Comtress 2 will not be able to\n"
            "run on some graphics cards from 2006 or before."
        )
        if error_reason:
            error_text += f"\n\nAdditional details:\n{error_reason}"
        logger.error(f"GPU minimum requirements not met: {error_reason}")
        return error_text, True, None

    if gpu_info and gpu_info["vendor_id"] == INTEL_VENDOR_ID:
        default_args += [
            "-force_vendor_id",
            "0x10DE",
            "-force_device_id",
            "0xFFFE",
            "-disable_d3d9_hacks",
        ]
        default_cmds += [
            "+mat_tonemapping_occlusion_use_stencil",
            "1",
            "+mat_disable_ps_patch",
            "1",
        ]

    if os.name == "nt":
        # disable nvidia vulkan due to DXGI swapchain issue on windows drivers
        if gpu_info and gpu_info["vendor_id"] == NVIDIA_VENDOR_ID:
            vk_supported = False

    extra_options_set = set(extra_options)
    banned_opts = []

    if "-nosentry" not in extra_options_set:
        default_args += ["-nobreakpad", "-nominidumps"]

    if not dedicated:
        res_w, res_h = get_native_resolution(exe_path)
        game_settings = _read_game_settings(exe_path.parent)

        # Resolution check
        res_opts = ["-w", "-width", "-h", "-height", "+mat_setvideomode"]
        has_res_opt = any(opt in extra_options_set for opt in res_opts)

        if not has_res_opt and res_w and res_h:
            conf_w = game_settings.get("ScreenWidth")
            conf_h = game_settings.get("ScreenHeight")
            if (
                not isinstance(conf_w, int)
                or not isinstance(conf_h, int)
                or conf_w <= 0
                or conf_h <= 0
                or conf_w > res_w
                or conf_h > res_h
            ):
                default_args += ["-w", str(res_w), "-h", str(res_h)]

        # Window mode check
        window_check_opts = [
            "-sw",
            "-startwindowed",
            "-window",
            "-windowed",
            "-noborder",
            "-full",
            "-fullscreen",
            "+mat_setvideomode",
        ]
        has_window_opt = any(opt in extra_options_set for opt in window_check_opts)

        if not has_window_opt:
            conf_windowed = game_settings.get("ScreenWindowed")
            conf_noborder = game_settings.get("ScreenNoBorder")
            if conf_windowed is None or conf_noborder is None:
                if os.name == "nt":
                    default_args += ["-sw", "-noborder"]
                else:
                    default_args += ["-full"]

    if os.name == "nt":
        if vk_supported and "-dx9" not in extra_options_set:
            default_args += ["-vulkan"]
    else:
        # force no OpenGL
        banned_opts += ["-gl"]

    banned_opts_set = set(banned_opts)
    has_banned_opts = False
    for opt in banned_opts:
        if opt in extra_options_set:
            has_banned_opts = True
            break

    if has_banned_opts:
        extra_options = [x for x in extra_options if x not in banned_opts_set]

    exe_file = exe_path.name if os.name == "nt" else f"./{exe_path.name}"
    cmd = [exe_file] + default_args + extra_options + default_cmds

    # Launch the game
    logger.info(f"Launching: {' '.join(cmd)}")
    env: dict[str, str] = {}
    if os.name == "posix":
        if not dedicated:
            if (
                os.getenv("WAYLAND_DISPLAY")
                or os.getenv("XDG_SESSION_TYPE") == "wayland"
            ):
                if "SDL_VIDEODRIVER" not in os.environ:
                    env["SDL_VIDEODRIVER"] = "x11"

            if "SDL_AUDIODRIVER" not in os.environ:
                env["SDL_AUDIODRIVER"] = "pulseaudio"

        slr_path = get_slr3_path(dedicated=dedicated, dest=dest)
        if slr_path is None or not slr_path.is_file():
            return (
                "Steam Linux Runtime not found. Install it through Steam (or by running steam steam://install/1628350)",
                True,
                None,
            )

        if not dedicated:
            cmd = [
                str(slr_path),
                "--devel",
                "--",
                " ".join(cmd),
            ]
        else:
            env[SLR3_ENV_NAME] = str(slr_path)

    original_cwd = os.getcwd()
    proc = None
    try:
        os.chdir(exe_path.parent)
        proc = run_non_blocking(cmd, cwd=exe_path.parent, env=env)
    except Exception as e:
        logger.error(f"Failed to launch game: {e}")
        return str(e), True, None
    finally:
        os.chdir(original_cwd)

    return None, False, proc


wait_game_exit_thread = None


def _wait_game_exit_inner(pid, callback):
    global wait_game_exit_thread
    try:
        p = psutil.Process(pid)
        while not sys.is_finalizing():
            try:
                p.wait(timeout=1)
                break
            except psutil.TimeoutExpired:
                pass
        wait_game_exit_thread = None
        if not sys.is_finalizing():
            callback()
    except Exception:
        wait_game_exit_thread = None


def wait_game_exit(pid, callback):
    global wait_game_exit_thread
    if wait_game_exit_thread is not None:
        return
    wait_game_exit_thread = threading.Thread(
        target=_wait_game_exit_inner, args=(pid, callback)
    )
    wait_game_exit_thread.start()


DEFAULT_TIME_LIMIT = 5


async def wait_game_running(
    time_limit: float = DEFAULT_TIME_LIMIT,
    dedicated: bool = False,
    dest: Path | None = None,
) -> int | None:
    game_exe_name = _get_game_exe_name(running_process=True, dedicated=dedicated)
    game_dir = get_game_dir(dest)

    interval = 0.2
    if time_limit == 0:
        time_limit = DEFAULT_TIME_LIMIT
    elif time_limit < 0:
        time_limit = interval

    while time_limit > 0:
        for p in psutil.process_iter(["pid", "name", "exe"]):
            if p.info["name"] != game_exe_name:
                continue
            try:
                exe = Path(p.info["exe"])
                if not exe.is_relative_to(game_dir):
                    continue
                return p.pid
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if time_limit <= interval:
            break
        before = timer()
        await asyncio.sleep(interval)
        time_limit -= timer() - before
    return None


def get_launch_options(dest: Path | None = None) -> list[str]:
    if not dest:
        dest = default_dest_dir()

    settings = read_settings(dest)
    extra_options = settings.get("opts")
    if extra_options and isinstance(extra_options, list):
        return extra_options
    return []


def set_launch_options(
    dest: Path | None = None, extra_options: list[str] | None = None
) -> None:
    if not dest:
        dest = default_dest_dir()

    settings = read_settings(dest)
    if extra_options and isinstance(extra_options, list):
        settings["opts"] = extra_options
    else:
        settings.pop("opts", None)
    write_settings(dest, settings)


def get_prerelease(dest: Path | None = None) -> str:
    if not dest:
        dest = default_dest_dir()

    settings = read_settings(dest)
    prerelease = settings.get("branch")
    if prerelease and isinstance(prerelease, str):
        return prerelease
    return ""


def set_prerelease(dest: Path | None = None, prerelease: str = "") -> None:
    if not dest:
        dest = default_dest_dir()

    settings = read_settings(dest)
    if prerelease:
        settings["branch"] = prerelease
    else:
        settings.pop("branch", None)
    write_settings(dest, settings)


def open_install_folder(dest: Path | None = None) -> None:
    if not dest:
        dest = default_dest_dir()

    game_dir = get_game_dir(dest)
    if game_dir.exists() and game_dir.is_dir():
        run_open(str(game_dir))


def change_install_folder(new_game_dir: Path, dest: Path | None = None):
    global _has_randomized_this_session
    with _game_dir_lock:
        if not dest:
            dest = default_dest_dir()

        old_game_dir = get_game_dir(dest)
        new_game_dir = new_game_dir.resolve()
        if new_game_dir == old_game_dir:
            logger.error(f"Cannot move game directory to itself: {new_game_dir}")
            return

        if new_game_dir.is_relative_to(old_game_dir):
            logger.error(
                f"Cannot move game directory to a subdirectory of itself: {new_game_dir}"
            )
            return

        if (
            os.name == "nt"
            and old_game_dir.exists()
            and _is_game_running_in_dir(old_game_dir)
        ):
            logger.error("Cannot move game directory while the game is running.")
            return

        selected_dir = new_game_dir
        selected_was_empty = False
        was_empty_or_missing = True
        did_randomize = False

        try:
            exe_name = "tc2_win64.exe" if os.name == "nt" else "tc2_linux64"

            # Check if the directory already contains the game executable
            is_game_dir = False
            if not new_game_dir.is_mount() and (new_game_dir / exe_name).exists():
                is_game_dir = True
            elif (
                new_game_dir.exists()
                and new_game_dir.is_dir()
                and not new_game_dir.is_mount()
                and not _dir_has_entries(new_game_dir)[0]
            ):
                selected_was_empty = True

            source_game_dir = new_game_dir

            # if the directory is a mount point or not empty (and not already a game dir), create a subdirectory
            if new_game_dir.is_mount() or (
                new_game_dir.exists()
                and _dir_has_entries(new_game_dir)[0]
                and not is_game_dir
            ):
                new_game_dir = new_game_dir / "Team Comtress 2"
                if os.name == "nt" and is_path_too_short(new_game_dir):
                    new_game_dir = new_game_dir.parent / "Games" / "Team Comtress 2"
                    did_randomize = True
                if (new_game_dir / exe_name).exists():
                    is_game_dir = True
                    source_game_dir = new_game_dir

            if os.name == "nt":
                if is_path_too_short(new_game_dir) or _is_legacy_tc2_dir_name(
                    new_game_dir
                ):
                    new_game_dir = _get_padded_game_dir(new_game_dir)
                    did_randomize = True

            new_game_dir = new_game_dir.resolve()
            if new_game_dir == old_game_dir:
                logger.error(f"Cannot move game directory to itself: {new_game_dir}")
                return

            if new_game_dir.is_relative_to(old_game_dir):
                logger.error(
                    f"Cannot move game directory to a subdirectory of itself: {new_game_dir}"
                )
                return

            if is_game_dir and new_game_dir != source_game_dir:
                try:
                    _rename_game_dir(source_game_dir, new_game_dir)
                    logger.info(
                        f"Renamed game directory from {source_game_dir} to {new_game_dir}"
                    )
                except OSError:
                    _copy_migrate_game_dir(source_game_dir, new_game_dir)
                    logger.info(
                        f"Migrated game directory from {source_game_dir} to {new_game_dir}"
                    )
                if not new_game_dir.exists() or not new_game_dir.is_dir():
                    raise Exception("Failed to rename directory")
            else:
                was_empty_or_missing = (
                    not new_game_dir.exists() or not _dir_has_entries(new_game_dir)[0]
                )
                if not _can_write_dir(new_game_dir):
                    raise PermissionError(f"Directory is not writable: {new_game_dir}")
        except Exception as e:
            logger.error(f"Invalid path '{new_game_dir}': {e}")
            return

        if not is_game_dir and old_game_dir.exists() and old_game_dir.is_dir():
            try:
                copytree(old_game_dir, new_game_dir, dirs_exist_ok=True)
            except Exception as e:
                logger.error(f"Failed to copy game directory: {e}")
                if was_empty_or_missing:
                    rmtree(new_game_dir, ignore_errors=True)
                return

            try:
                rmtree(old_game_dir)
            except Exception as e:
                logger.warning(
                    f"Copied game directory to {new_game_dir}, could not fully remove old directory {old_game_dir}: {e}"
                )
                rmtree(old_game_dir, ignore_errors=True)

        if (
            selected_was_empty
            and selected_dir != new_game_dir
            and not new_game_dir.is_relative_to(selected_dir)
        ):
            try:
                selected_dir.rmdir()
            except Exception:
                pass

        if did_randomize:
            _has_randomized_this_session = True

        settings = read_settings(dest)
        settings["game_dir"] = str(new_game_dir)
        write_settings(dest=dest, settings=settings)


def uninstall(reset_settings: bool, dest: Path | None = None) -> bool:
    if not dest:
        dest = default_dest_dir()

    game_dir = get_game_dir(dest)
    if game_dir.exists() and game_dir.is_dir():
        try:
            rmtree(game_dir)
        except Exception as e:
            logger.error(f"Failed to uninstall game directory: {e}")
            return False

    if not reset_settings:
        return True

    # make sure it's not already uninstalled
    if not dest.exists() or not dest.is_dir():
        return True

    # make sure it's a game directory
    settings = read_settings(dest)
    if not settings:
        return True

    try:
        rmtree(dest)
        return True
    except Exception as e:
        logger.error(f"Failed to uninstall launcher: {e}")

    return False
