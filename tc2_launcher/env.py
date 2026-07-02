import os
import ssl
import sys
from contextlib import contextmanager
from pathlib import Path

import certifi
import psutil
import vdf

from tc2_launcher import logger
from tc2_launcher.utils import DEV_INSTANCE


def get_steam_libraries() -> dict[int, Path]:
    if os.name == "nt":
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam")
        steam_path_str, _ = winreg.QueryValueEx(key, "SteamPath")
        steam_path = Path(steam_path_str)
        winreg.CloseKey(key)
    else:
        steam_path = Path.home() / ".steam" / "steam"
    library_folders = steam_path / "config" / "libraryfolders.vdf"
    with library_folders.open("r", encoding="utf-8") as f:
        data = vdf.load(f)
    library_data = {}
    for library in data["libraryfolders"].values():
        path = Path(library["path"])
        if not path.exists() or not path.is_dir():
            continue
        dirs = [
            d for d in path.iterdir() if d.is_dir() and d.name.lower() == "steamapps"
        ]
        dirs = list(sorted(dirs, key=lambda x: x.name, reverse=True))
        steamapps = dirs[0]
        for app in library["apps"].keys():
            library_data[int(app)] = steamapps
    return library_data


def get_steam_app(app_id: int) -> Path | None:
    libraries = get_steam_libraries()
    steamapps_path = libraries.get(app_id)
    if steamapps_path is None:
        return None
    appmanifest = steamapps_path / f"appmanifest_{app_id}.acf"
    with appmanifest.open("r", encoding="utf-8") as f:
        data = vdf.load(f)
    app_data = data["AppState"]
    install_dir = steamapps_path / "common" / app_data["installdir"]
    return install_dir


SLR3_APPID = 1628350


def get_slr3_path(dedicated: bool = False, dest: Path | None = None) -> Path | None:
    if dedicated:
        if not dest:
            dest = Path.home() / ".local/share/Steam/steamcmd"
        slr3_dir = dest / "SteamLinuxRuntime_sniper"
    else:
        slr3_dir = get_steam_app(SLR3_APPID)
    if slr3_dir is None:
        return None
    return slr3_dir / "run"


SLR3_ENV_NAME = "SLR_SNIPER_PATH"


def get_safe_env(preserve_pyi: bool = False) -> dict:
    new_env = os.environ.copy()
    if os.name == "posix":
        if not DEV_INSTANCE:
            lp_orig = new_env.get("LD_LIBRARY_PATH_ORIG")
            if lp_orig is not None:
                new_env["LD_LIBRARY_PATH"] = lp_orig
            else:
                new_env.pop("LD_LIBRARY_PATH", None)

    if not DEV_INSTANCE:
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            try:
                meipass_path = Path(meipass).resolve()
                meipass_lower = str(meipass).lower()
                for key, value in list(new_env.items()):
                    is_pyi = key.startswith("PYI_") or key.startswith("_PYI_")
                    if is_pyi and not preserve_pyi:
                        new_env.pop(key, None)
                        continue

                    if not value or is_pyi:
                        continue
                    if meipass_lower not in value.lower():
                        continue

                    paths = value.split(os.pathsep)
                    new_paths = []
                    changed = False
                    for p in paths:
                        if not p:
                            continue
                        try:
                            if (
                                Path(p).resolve().is_relative_to(meipass_path)
                                or "_mei" in p.lower()
                            ):
                                changed = True
                                continue
                        except Exception:
                            pass
                        new_paths.append(p)

                    if changed:
                        if not new_paths:
                            new_env.pop(key, None)
                        else:
                            new_env[key] = os.pathsep.join(new_paths)
            except Exception as e:
                logger.error(f"Failed to sanitize environment variables: {e}")

    return new_env


@contextmanager
def restore_system_env():
    orig_env = os.environ.copy()
    safe_env = get_safe_env()

    to_set = {k: v for k, v in safe_env.items() if v != orig_env.get(k)}
    to_remove = [k for k in orig_env if k not in safe_env]

    try:
        os.environ.update(to_set)
        for key in to_remove:
            os.environ.pop(key, None)

        yield
    finally:
        for key in to_set.keys():
            if key in orig_env:
                os.environ[key] = orig_env[key]
            else:
                os.environ.pop(key, None)
        for key in to_remove:
            os.environ[key] = orig_env[key]


def get_desktop_environment() -> str | None:
    if os.name != "posix":
        return None
    xdg_desktop = os.getenv("XDG_CURRENT_DESKTOP")
    if not xdg_desktop:
        return None
    xdg_desktops = xdg_desktop.lower().split(":")
    if "gnome" in xdg_desktops or "GNOME_DESKTOP_SESSION_ID" in os.environ:
        return "gnome"
    if "kde" in xdg_desktops or "KDE_FULL_SESSION" in os.environ:
        return "kde"
    return xdg_desktops[0]


QT_DESKTOPS = {"kde", "plasma", "lxqt", "lxde"}
GTK_DESKTOPS = {"gnome", "xfce", "cinnamon", "mate", "budgie"}


def is_qt_environment() -> bool:
    desktop_env = get_desktop_environment()
    if desktop_env in QT_DESKTOPS:
        return True
    if desktop_env in GTK_DESKTOPS:
        return False
    return os.getenv("QT_QPA_PLATFORM", "") != ""


def is_steam_running():
    if os.name == "nt":
        for p in psutil.process_iter(["name"]):
            try:
                if "steam.exe" == p.info["name"].lower():
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return False
    else:
        pid_path = Path.home() / ".steam/steam.pid"
        if not pid_path.exists():
            return False
        with open(pid_path) as pid_file:
            pid = pid_file.read()
        try:
            steam_proc = psutil.Process(int(pid))
        except:
            return False
        return steam_proc.name() == "steam"


def get_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()

    if not context.get_ca_certs():
        context.load_verify_locations(cafile=certifi.where())

    return context


def register_url_handler_windows():
    try:
        import sys
        import winreg

        exe_path = sys.executable
        key_path = r"Software\Classes\comtress"

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:comtress Protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")

            with winreg.CreateKey(key, r"shell\open\command") as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, f'"{exe_path}" "%1"')
    except Exception as e:
        logger.error(f"Failed to register URL handler on Windows: {e}")


def register_url_handler_linux():
    try:
        import subprocess
        import sys
        from pathlib import Path

        exe_path = sys.executable

        apps_dir = Path.home() / ".local" / "share" / "applications"
        apps_dir.mkdir(parents=True, exist_ok=True)

        desktop_file = apps_dir / "tc2-comtress-url-handler.desktop"

        content = f"""[Desktop Entry]
Name=Team Comtress URL Handler
Exec="{exe_path}" %u
Type=Application
Terminal=false
MimeType=x-scheme-handler/comtress;
NoDisplay=true
"""
        with open(desktop_file, "w") as f:
            f.write(content)

        subprocess.run(["update-desktop-database", str(apps_dir)], capture_output=True)
    except Exception as e:
        logger.error(f"Failed to register URL handler on Linux: {e}")


def register_url_handler():
    if DEV_INSTANCE:
        return
    if os.name == "nt":
        register_url_handler_windows()
    elif os.name == "posix":
        register_url_handler_linux()
