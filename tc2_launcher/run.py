import os
import sys
import json
import stat
from pathlib import Path
from typing import Optional, Tuple
from shutil import rmtree

import requests
import subprocess
import zipfile


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

    if not base.exists():
        base.mkdir(parents=True, exist_ok=True)

    return base / "TC2Launcher"


def _get_latest_release(repo: str) -> dict:
    resp = requests.get(
        f"https://api.github.com/repos/{repo}/releases/latest", timeout=30
    )
    resp.raise_for_status()
    return resp.json()


def _find_asset(release: dict, asset_filter: str) -> Optional[Tuple[str, str]]:
    assets = release.get("assets", [])
    for a in assets:
        name = a.get("name", "")
        if asset_filter.lower() in name.lower():
            return name, a.get("browser_download_url")
    return None


def update_self(current_version: str) -> bool:
    try:
        release = _get_latest_release(LAUNCHER_REPO)
        tag = release.get("tag_name", "").lstrip("v")
    except Exception as e:
        print(f"ERROR: Failed to get self-update info: {e}")
        return False

    if tag == current_version:
        return False

    print(f"Self-update available: {current_version} -> {tag}")

    if os.name == "nt":
        asset_filter = ".exe"
    else:
        asset_filter = "-linux"
    asset = _find_asset(release, asset_filter)
    if not asset:
        print(
            f"ERROR: No asset matching '{asset_filter}' found in self-update release {tag}."
        )
        return False

    asset_name, download_url = asset

    dest_dir = default_dest_dir()
    download_path = dest_dir / "update" / tag / asset_name
    print(f"Downloading self-update...")
    try:
        _download(download_url, download_path)
    except Exception as e:
        print(f"ERROR: Failed to download self-update: {e}")
        return False
    print("Self-update download complete.")

    current_path = Path(sys.argv[0]).resolve()
    print("Launching self-update...")
    try:
        filtered_args = [arg for arg in sys.argv[1:] if arg != "--replace"]
        if os.name == "posix":
            download_path.chmod(download_path.stat().st_mode | stat.S_IEXEC)
        run_non_blocking([str(download_path), "--replace", str(current_path)] + filtered_args)
        sys.exit(0)
        return True
    except Exception as e:
        print(f"Failed to launch self-update: {e}")
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _state_path(dest: Path) -> Path:
    return dest / "state.json"


def read_state(dest: Path) -> dict:
    path = _state_path(dest)
    return _read_data(path)


def write_state(dest: Path, state: dict) -> None:
    path = _state_path(dest)
    _write_data(path, state)


def _settings_path(dest: Path) -> Path:
    return dest / "settings.json"


def read_settings(dest: Path) -> dict:
    path = _settings_path(dest)
    return _read_data(path)


def write_settings(dest: Path, settings: dict) -> None:
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


# https://stackoverflow.com/a/54748564
class ZipFileWithPermissions(zipfile.ZipFile):
    """ Custom ZipFile class handling file permissions. """
    def _extract_member(self, member, targetpath, pwd):
        if not isinstance(member, zipfile.ZipInfo):
            member = self.getinfo(member)

        targetpath = super()._extract_member(member, targetpath, pwd)

        attr = member.external_attr >> 16
        if attr != 0:
            os.chmod(targetpath, attr)
        return targetpath


def _extract_zip_if_needed(zip_path: Path, extract_dir: Path):
    extract_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        with ZipFileWithPermissions(zip_path, "r") as zf:
            zf.extractall(extract_dir)
    else:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)


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
        release = _get_latest_release(TC2_REPO)
        tag = release.get("tag_name")
        if not tag:
            print("ERROR: Could not determine release tag.")
            return fail_code
    except Exception as e:
        print(f"ERROR: Failed to fetch latest release info: {e}")
        return fail_code

    state = read_state(dest)
    current_tag = state.get("tag")
    if not force and current_tag == tag and fail_code == 1:
        print("Latest asset already downloaded.")
        return 0

    if os.name == "nt":
        asset_filter = "game-win.zip"
    else:
        asset_filter = "game-linux.zip"
    asset = _find_asset(release, asset_filter)
    if not asset:
        print(f"ERROR: No asset matching '{asset_filter}' found in release {tag}.")
        return fail_code

    asset_name, download_url = asset

    print(f"Latest release tag: {tag}")
    print(f"Current release tag: {current_tag}")
    print(f"Selected asset: {asset_name}")
    print(f"Destination: {dest}")

    asset_path = dest / asset_name
    game_dir = dest / "game"

    print(f"Downloading latest asset to {asset_path}...")
    try:
        _download(download_url, asset_path)
    except Exception as e:
        print(f"ERROR: Failed to download asset: {e}")
        return fail_code
    print("Download complete.")

    print("Extracting asset...")
    if asset_name.lower().endswith(".zip") and asset_path.exists():
        _extract_zip_if_needed(asset_path, game_dir)

    if not game_dir.exists():
        print(f"ERROR: Game directory '{game_dir}' does not exist after extraction.")
        return fail_code

    print("Extraction complete.")

    exe_path = _get_game_exe(dest)
    if not exe_path:
        print(f"ERROR: Could not locate game executable after update.")
        return -2

    state["tag"] = tag
    write_state(dest, state)

    return 0


def run_non_blocking(cmd: list[str], cwd: Optional[Path] = None) -> None:
    if os.name == "posix":
        cmd.insert(0, "nohup")
        cmd = " ".join(cmd) if isinstance(cmd, list) else cmd

    try:
        if os.name == "nt":
            subprocess.Popen(
                cmd,
                cwd=str(cwd) if cwd else None,
                shell=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_DEFAULT_ERROR_MODE,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        elif os.name == "posix":
            subprocess.Popen(
                cmd,
                cwd=str(cwd) if cwd else None,
                shell=True,
                start_new_session=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
    except Exception as e:
        print(f"Failed to run command {' '.join(cmd)}: {e}")


def _get_game_exe(dest: Path | None) -> Optional[Path]:
    if not dest:
        dest = default_dest_dir()

    game_dir = dest / "game"

    # Determine executable based on platform
    exe_path = None
    if sys.platform.startswith("win"):
        exe_path = game_dir / "tc2_win64.exe"
    else:
        exe_path = game_dir / "tc2.sh"

    if exe_path and exe_path.exists():
        return exe_path
    return None


def launch_game(
    dest: Path | None = None,
    extra_opts: Optional[list[str]] = None,
) -> None:
    if not dest:
        dest = default_dest_dir()

    exe_path = _get_game_exe(dest)
    if not exe_path:
        print(f"Could not locate game executable '{exe_path}'")
        return

    # Resolve options with persistence
    settings = read_settings(dest)
    # TODO: condebug prevents an access violation crash to stdout or something, need to fix the Popen call eventually
    if sys.platform.startswith("win"):
        default_args = ["-steam", "-particles", "1", "-condebug"]
        default_cmds = ["+ip", "127.0.0.1"]
    else:
        default_args = ["-condebug"]
        default_cmds = []
    if not extra_opts:
        extra_opts = settings.get("opts")
    if not extra_opts or not isinstance(extra_opts, list):
        extra_opts = []
    user_opts_set = set(extra_opts)
    noborder_check_opts = ["-sw", "-fullscreen", "-windowed", "-noborder"]
    use_noborder = True
    for opt in noborder_check_opts:
        if opt in user_opts_set:
            use_noborder = False
    if use_noborder:
        default_args += ["-sw", "-noborder"]
    cmd = [str(exe_path)] + default_args + extra_opts + default_cmds

    # Launch the game
    print(f"Launching: {' '.join(cmd)}")
    try:
        run_non_blocking(cmd, cwd=exe_path.parent)
    except Exception as e:
        print(f"Failed to launch game: {e}")


def get_launch_options(dest: Path | None = None) -> list[str]:
    if not dest:
        dest = default_dest_dir()

    settings = read_settings(dest)
    options = settings.get("opts")
    if options and isinstance(options, list):
        return options
    return []


def set_launch_options(
    dest: Path | None = None, options: list[str] | None = None
) -> None:
    if not dest:
        dest = default_dest_dir()

    settings = read_settings(dest)
    if options and isinstance(options, list):
        settings["opts"] = options
    else:
        settings.pop("opts", None)
    write_settings(dest, settings)


def open_install_folder(dest: Path | None = None) -> None:
    if not dest:
        dest = default_dest_dir()

    game_dir = dest / "game"
    if game_dir.exists() and game_dir.is_dir():
        if sys.platform.startswith("win"):
            os.startfile(game_dir)
        elif sys.platform.startswith("darwin"):
            subprocess.Popen(["open", game_dir])
        else:
            subprocess.Popen(["xdg-open", game_dir])


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
        print(f"Failed to uninstall launcher: {e}")

    return False
