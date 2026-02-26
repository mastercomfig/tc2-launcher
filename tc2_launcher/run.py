import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import webbrowser
import zipfile
from contextlib import contextmanager
from pathlib import Path
from shutil import copytree, rmtree
from time import sleep
from timeit import default_timer as timer
from typing import Callable

import psutil
import requests

from tc2_launcher import logger
from tc2_launcher.utils import DEV_INSTANCE

TC2_REPO = "mastercomfig/tc2"
LAUNCHER_REPO = "mastercomfig/tc2-launcher"


def default_dest_dir() -> Path:
    if sys.platform == "darwin":
        env = "XDG_DATA_HOME"
        fallback = Path.home() / "Library" / "Application Support"
    elif os.name == "posix":
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
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "TC2Launcher",
}


def _get_latest_release(dest: Path | None, repo: str) -> dict:
    if repo == TC2_REPO:
        if not dest:
            dest = default_dest_dir()

        settings = read_settings(dest)
        branch = settings.get("branch")
        if branch == "prerelease":
            resp = requests.get(
                f"https://api.github.com/repos/{repo}/releases",
                timeout=30,
                params={"per_page": 1},
                headers=github_api_headers,
            )
            resp.raise_for_status()
            releases = resp.json()
            return releases[0] if releases else {}
        elif branch and branch[0].isdigit():
            try:
                resp = requests.get(
                    f"https://api.github.com/repos/{repo}/releases/tags/{branch}",
                    timeout=30,
                    headers=github_api_headers,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.error(
                    f"Failed to get release for branch {branch}, falling back to latest: {e}"
                )

    resp = requests.get(
        f"https://api.github.com/repos/{repo}/releases/latest",
        timeout=30,
        headers=github_api_headers,
    )
    resp.raise_for_status()
    return resp.json()


def _find_asset(release: dict, asset_filter: str) -> tuple[str, str, str] | None:
    assets = release.get("assets", [])
    for a in assets:
        name = a.get("name", "")
        if asset_filter.lower() in name.lower():
            return name, a.get("browser_download_url"), a.get("digest")
    return None


def update_self(current_version: str) -> bool:
    try:
        release = _get_latest_release(None, LAUNCHER_REPO)
        tag = release.get("tag_name", "").lstrip("v")
    except Exception as e:
        logger.error(f"Failed to get self-update info: {e}")
        return False

    if tag == current_version:
        return False

    logger.info(f"Self-update available: {current_version} -> {tag}")

    if os.name == "nt":
        asset_filter = ".exe"
    else:
        asset_filter = "-linux"
        if importlib.util.find_spec("qtpy") is not None:
            asset_filter += "-qt"
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
        _download(download_url, download_path)
    except Exception as e:
        logger.error(f"Failed to download self-update: {e}")
        return False
    logger.info("Self-update download complete.")

    current_path = Path(sys.argv[0]).resolve()
    logger.info("Launching self-update...")
    try:
        filtered_args = [arg for arg in sys.argv[1:] if arg != "--replace"]
        if os.name == "posix":
            download_path.chmod(download_path.stat().st_mode | stat.S_IEXEC)
        run_non_blocking(
            [str(download_path), "--replace", str(current_path)] + filtered_args
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


def _download(url: str, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
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

        targetpath = super()._extract_member(member, targetpath, pwd)

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


def update_archive(
    dest: Path | None = None,
    force: bool = False,
) -> int:
    if not dest:
        dest = default_dest_dir()

    exe_path = _get_game_exe(dest)
    if exe_path is not None:
        fail_code = 1
    else:
        fail_code = 2

    try:
        release = _get_latest_release(dest, TC2_REPO)
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

    if os.name == "nt":
        asset_filter = "game-win.zip"
    else:
        asset_filter = "game-linux.zip"

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
            _download(download_url, asset_path)
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

    exe_path = _get_game_exe(dest)
    if not exe_path:
        logger.error("Could not locate game executable after update.")
        return -2

    logger.info("Extraction complete.")

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


def get_safe_env() -> dict:
    new_env = os.environ.copy()
    if os.name == "posix":
        if "LD_LIBRARY_PATH_ORIG" in new_env:
            new_env["LD_LIBRARY_PATH"] = new_env["LD_LIBRARY_PATH_ORIG"]
        elif "LD_LIBRARY_PATH" in new_env:
            del new_env["LD_LIBRARY_PATH"]
    elif os.name == "nt":
        if not DEV_INSTANCE and hasattr(sys, "_MEIPASS"):
            meipass = getattr(sys, "_MEIPASS", "")
            if meipass:
                paths = new_env.get("PATH", "").split(os.pathsep)
                paths = [
                    p
                    for p in paths
                    if p
                    and os.path.normpath(p).lower() != os.path.normpath(meipass).lower()
                ]
                new_env["PATH"] = os.pathsep.join(paths)
    return new_env


@contextmanager
def restore_system_env():
    old_lib_path = os.environ.get("LD_LIBRARY_PATH")

    try:
        safe_env = get_safe_env()
        new_lib_path = safe_env.get("LD_LIBRARY_PATH")
        if new_lib_path is not None:
            os.environ["LD_LIBRARY_PATH"] = new_lib_path
        elif old_lib_path is not None:
            os.environ.pop("LD_LIBRARY_PATH")

        yield
    finally:
        if old_lib_path is not None:
            os.environ["LD_LIBRARY_PATH"] = old_lib_path
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)


def run_open(url: str):
    if os.name == "nt":
        webbrowser.open(url)
    else:
        with restore_system_env():
            webbrowser.open(url)


def run_blocking(cmd: list[str], cwd: Path | None = None) -> None:
    if os.name == "nt":
        subprocess.run(cmd, env=get_safe_env(), cwd=cwd)
    else:
        cmd = " ".join(cmd) if isinstance(cmd, list) else cmd
        subprocess.run(cmd, env=get_safe_env(), cwd=cwd, shell=True)


def run_non_blocking(cmd: list[str], cwd: Path | None = None) -> None:
    if os.name == "posix":
        cmd.insert(0, "nohup")
        cmd = " ".join(cmd) if isinstance(cmd, list) else cmd

    try:
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = (
                subprocess.CREATE_NO_WINDOW
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
            )
            subprocess.Popen(
                cmd,
                env=get_safe_env(),
                cwd=cwd,
                startupinfo=startupinfo,
                creationflags=creationflags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        elif os.name == "posix":
            subprocess.Popen(
                cmd,
                env=get_safe_env(),
                cwd=cwd,
                shell=True,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as e:
        logger.error(f"Failed to run command {' '.join(cmd)}: {e}")


def get_game_dir(dest: Path | None = None) -> Path:
    settings = read_settings(dest)

    user_game_dir = settings.get("game_dir")
    if user_game_dir:
        try:
            dest = Path(user_game_dir).resolve()
            dest.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to use specified game directory '{dest}': {e}")
        if dest and dest.exists() and dest.is_dir():
            return dest

    default_dest = default_dest_dir()
    default_game_dest = default_dest / "game"
    if not dest or dest == default_dest:
        if os.name == "nt" and not default_game_dest.exists():
            drive = Path.home().drive
            dest = Path(f"{drive}\\") / "tc2"
            return dest
        else:
            return default_game_dest

    return dest / "game"


def _get_game_exe_name(running_process: bool = False) -> str:
    # Determine executable name based on platform
    if os.name == "nt":
        return "tc2_win64.exe"
    else:
        return "tc2_linux64" if running_process else "tc2.sh"


def _get_game_exe(dest: Path | None) -> Path | None:
    if not dest:
        dest = default_dest_dir()

    game_dir = get_game_dir(dest)

    # Determine executable based on platform
    exe_path = game_dir / _get_game_exe_name()

    if exe_path and exe_path.exists():
        return exe_path
    return None


def launch_game(
    dest: Path | None = None,
    extra_options: list[str] | None = None,
) -> None:
    if not dest:
        dest = default_dest_dir()

    exe_path = _get_game_exe(dest)
    if not exe_path:
        logger.error(f"Could not locate game executable '{exe_path}'")
        return

    # Resolve options with persistence
    settings = read_settings(dest)
    default_args = [
        "-steam",
        "-particles",
        "1",
        "-condebug",
        "-nobreakpad",
        "-nominidumps",
    ]
    if os.name == "nt":
        default_cmds = ["+ip", "127.0.0.1"]
    else:
        default_args += ["-gathermod"]
        default_cmds = []
    if not extra_options:
        extra_options = settings.get("opts")
    if not extra_options or not isinstance(extra_options, list):
        extra_options = []
    if os.name == "nt":
        noborder_check_opts = ["-sw", "-windowed", "-noborder", "-full", "-fullscreen"]
        extra_options_set = set(extra_options)
        use_noborder = True
        for opt in noborder_check_opts:
            if opt in extra_options_set:
                use_noborder = False
        if use_noborder:
            default_args += ["-sw", "-noborder"]
    cmd = [str(exe_path)] + default_args + extra_options + default_cmds

    # Launch the game
    logger.info(f"Launching: {' '.join(cmd)}")
    try:
        run_non_blocking(cmd, cwd=exe_path.parent)
    except Exception as e:
        logger.error(f"Failed to launch game: {e}")


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


def wait_game_running(time_limit: float = DEFAULT_TIME_LIMIT) -> int | None:
    game_exe_name = _get_game_exe_name(running_process=True)
    game_dir = get_game_dir()

    interval = 0.2
    if time_limit == 0:
        time_limit = DEFAULT_TIME_LIMIT
    elif time_limit < 0:
        time_limit = interval

    while time_limit > 0:
        for p in psutil.process_iter(["pid", "name", "exe"]):
            if p.info["name"] != game_exe_name:
                continue
            exe = Path(p.info["exe"])
            if not exe.is_relative_to(game_dir):
                continue
            return p.pid
        if time_limit <= interval:
            break
        before = timer()
        sleep(interval)
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
        if os.name == "nt":
            os.startfile(game_dir)
        elif sys.platform == "darwin":
            run_non_blocking(["open", game_dir])
        else:
            opener = "xdg-open"
            xdg_desktop = os.getenv("XDG_CURRENT_DESKTOP", "").split(":")
            if "KDE" in xdg_desktop or "KDE_FULL_SESSION" in os.environ:
                opener = "kde-open5"
            run_non_blocking([opener, game_dir])


def change_install_folder(new_game_dir: Path):
    try:
        # if the directory is a mount point or not empty, create a subdirectory
        if new_game_dir.is_mount() or any(new_game_dir.iterdir()):
            new_game_dir = new_game_dir / "tc2"
        new_game_dir = new_game_dir.resolve()
        new_game_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"Invalid path '{new_game_dir}': {e}")
        return

    old_game_dir = get_game_dir()
    if new_game_dir == old_game_dir:
        return
    if old_game_dir.exists():
        try:
            copytree(old_game_dir, new_game_dir, dirs_exist_ok=True)
            rmtree(old_game_dir)
        except Exception as e:
            logger.error(f"Failed to move game directory: {e}")
            return

    settings = read_settings()
    settings["game_dir"] = str(new_game_dir)
    write_settings(dest=None, settings=settings)


def uninstall_launcher(dest: Path | None = None) -> bool:
    if not dest:
        dest = default_dest_dir()

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
