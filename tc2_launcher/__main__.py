import argparse
import multiprocessing
import sys
from pathlib import Path
from shutil import copyfile
from time import sleep
from timeit import default_timer as timer

from tc2_launcher.gui import start_gui
from tc2_launcher.run import (
    DEV_INSTANCE,
    clean_self_update,
    default_dest_dir,
    launch_game,
    set_launch_options,
    update_archive,
    update_self,
)

version = "0.6.0"


def main():
    launch_gui = False
    if len(sys.argv) >= 3 and sys.argv[1] == "--replace":
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
                print("Self-update applied successfully.")
        except Exception as e:
            print(f"ERROR: Failed to apply self-update: {e}")

        launch_gui = len(sys.argv) == 3
    else:
        if not DEV_INSTANCE and update_self(version):
            sys.exit(0)
            return

        launch_gui = len(sys.argv) == 1

        clean_self_update()

    parser = argparse.ArgumentParser(description=f"TC2 Launcher v{version}")
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
        start_gui()
        return

    args = parser.parse_args()

    dest_dir = args.dest
    dest = None
    if dest_dir:
        try:
            dest = Path(dest_dir).resolve()
        except Exception as e:
            print(f"ERROR: Invalid destination path '{dest_dir}': {e}")
            return
        if not dest.exists():
            try:
                dest.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                print(f"ERROR: Failed to create destination directory '{dest}': {e}")
                return
        elif not dest.is_dir():
            print(f"ERROR: Destination '{dest}' exists and is not a directory.")
            return
    else:
        dest = default_dest_dir()

    update_archive(
        dest=dest,
        force=args.force,
    )

    # Persistence of options
    if args.save_opts:
        set_launch_options(dest=dest, extra_options=args.opts)

    if args.launch:
        launch_game(dest=dest, extra_options=args.opts)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
