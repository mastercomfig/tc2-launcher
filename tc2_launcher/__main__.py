import argparse
import multiprocessing
import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from shutil import copyfile
from time import sleep
from timeit import default_timer as timer
from typing import Optional

if os.name == "posix":
    import stat

from tc2_launcher import logger
from tc2_launcher.gui import start_gui, start_gui_separate
from tc2_launcher.run import (
    clean_self_update,
    default_dest_dir,
    launch_game,
    set_launch_options,
    update_archive,
    update_self,
)
from tc2_launcher.utils import DEV_INSTANCE, VERSION_STR

updater_thread_queue: Optional[queue.Queue] = None
should_launch_updater = True
use_updater_gui = False

# clear out custom search path
if sys.platform == "win32" and not DEV_INSTANCE:
    import ctypes

    ctypes.windll.kernel32.SetDllDirectoryW(None)


def updater_thread():
    if not use_updater_gui:
        return
    global updater_thread_queue
    sleep(0.5)
    if not should_launch_updater:
        return
    p, q = start_gui_separate("update", frameless=True, easy_drag=True)
    if p is None:
        return
    if updater_thread_queue:
        q.put(updater_thread_queue.get())
    updater_thread_queue = None


def start_updater_gui():
    if not use_updater_gui:
        return
    global updater_thread_queue
    updater_thread_queue = queue.Queue()
    threading.Thread(target=updater_thread).start()


def close_updater_gui():
    if not use_updater_gui:
        return
    global updater_thread_queue
    if updater_thread_queue:
        updater_thread_queue.put("close")


def main():
    global should_launch_updater
    launch_gui = False
    if len(sys.argv) >= 3 and sys.argv[1] == "--replace":
        launch_gui = len(sys.argv) == 3

        # Replacement mode after self-update
        try:
            original_path = Path(sys.argv[2]).resolve()
            if original_path.exists() and original_path.is_file():
                current_path = Path(sys.argv[0]).resolve()
                # wait for the original process to exit
                time_limit = 5
                success = False
                last_exc = None
                while time_limit > 0:
                    try:
                        # attempt to delete the original file
                        original_path.unlink(missing_ok=True)
                        success = True
                        break
                    except Exception as e:
                        last_exc = e
                        # wait a moment before trying again
                        before = timer()
                        sleep(0.1)
                        time_limit -= timer() - before
                if not success:
                    raise last_exc or Exception("Unknown error deleting original file")
                # replace the original file with the current file
                copyfile(current_path, original_path)
                if os.name == "posix":
                    original_path.chmod(
                        original_path.stat().st_mode
                        | stat.S_IEXEC
                        | stat.S_IXGRP
                        | stat.S_IXOTH
                    )
                logger.info("Self-update applied successfully.")
        except Exception as e:
            logger.error(f"Failed to apply self-update: {e}")
    else:
        launch_gui = len(sys.argv) == 1
        if launch_gui:
            start_updater_gui()

        should_exit = False

        if not DEV_INSTANCE and update_self():
            should_exit = True

        should_launch_updater = False
        close_updater_gui()

        if should_exit:
            return

        clean_self_update()

    parser = argparse.ArgumentParser(description=f"TC2 Launcher v{VERSION_STR}")
    parser.add_argument(
        "--vulkan-info",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dx-info",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    # Handle hidden info flags early
    temp_args, _ = parser.parse_known_args()
    if temp_args.vulkan_info:
        logger.setup_silent_logger()
        from tc2_launcher.hardware import _get_vulkan_info_internal
        import json
        is_supported, gpu_info, error_msg = _get_vulkan_info_internal()
        print(json.dumps({"is_supported": is_supported, "gpu_info": gpu_info, "error_msg": error_msg}))
        return
    if temp_args.dx_info:
        logger.setup_silent_logger()
        from tc2_launcher.hardware import get_dx_info
        import json
        is_supported, gpu_info, error_msg = get_dx_info()
        print(json.dumps({"is_supported": is_supported, "gpu_info": gpu_info, "error_msg": error_msg}))
        return

    parser.add_argument(
        "--dest",
        default=None,
        help="Destination folder to write data to. Defaults to platform-specific data location",
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="Skip launching GUI and run in command-line mode only. This is implied if any args are provided.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download base archive even if latest is installed",
    )
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Launch the game after ensuring latest archive",
    )
    parser.add_argument(
        "--save-opts",
        action="store_true",
        help="Persist provided launch options as defaults in settings.json",
    )
    parser.add_argument(
        "--opts",
        nargs=argparse.REMAINDER,
        help="User launch options. Use at the very end of the command line, as all remaining arguments are used as launch options.",
    )

    if launch_gui:
        logger.setup_logger(default_dest_dir())
        start_gui()
        return

    args = parser.parse_args()

    dest_dir = args.dest
    dest = None
    if dest_dir:
        try:
            dest = Path(dest_dir).resolve()
        except Exception as e:
            logger.error(f"Invalid destination path '{dest_dir}': {e}")
            return
        if not dest.exists():
            try:
                dest.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.error(f"Failed to create destination directory '{dest}': {e}")
                return
        elif not dest.is_dir():
            logger.error(f"Destination '{dest}' exists and is not a directory.")
            return
    else:
        dest = default_dest_dir()

    logger.setup_logger(dest)

    update_archive(
        dest=dest,
        force=args.force,
    )

    # Persistence of options
    if args.save_opts:
        set_launch_options(dest=dest, extra_options=args.opts)

    if args.launch:
        err, should_print = launch_game(dest=dest, extra_options=args.opts)
        if err and should_print:
            logger.error(err)


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    multiprocessing.freeze_support()
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    # TODO: pyinstaller workaround for XDG_DATA_DIRS until #9422 is merged
    xdg_data_dirs = os.getenv("XDG_DATA_DIRS")
    if xdg_data_dirs and os.pathsep not in xdg_data_dirs:
        os.environ["XDG_DATA_DIRS"] = (
            xdg_data_dirs
            + os.pathsep
            + "/usr/local/share/"
            + os.pathsep
            + "/usr/share/"
        )
    try:
        main()
    except Exception:
        logger.error(traceback.format_exc())
        sys.exit(1)
