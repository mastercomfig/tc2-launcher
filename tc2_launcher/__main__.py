import argparse
import multiprocessing
import time
from pathlib import Path
import sys

from tc2_launcher.gui import start_gui
from tc2_launcher.run import update_archive
from tc2_launcher.run import launch_game
from tc2_launcher.run import set_launch_options
from tc2_launcher.run import default_dest_dir
from tc2_launcher.run import update_self

version = "0.2.0"


def main():
    launch_gui = False
    if len(sys.argv) >= 3 and sys.argv[1] == "--replace":
        # Replacement mode after self-update
        original_path = Path(sys.argv[2]).resolve()
        current_path = Path(sys.argv[0]).resolve()
        try:
            # Wait a moment to ensure the original process has exited
            time.sleep(2)
            # Replace the original file with the current file
            original_path.unlink()
            current_path.replace(original_path)
            print("Self-update applied successfully.")
        except Exception as e:
            print(f"ERROR: Failed to apply self-update: {e}")

        launch_gui = len(sys.argv) == 3
    else:
        if update_self(version):
            sys.exit(0)
            return

        launch_gui = len(sys.argv) == 1


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
        help="User launch options",
    )
    
    if launch_gui:
        start_gui()
        return

    args = parser.parse_args()

    dest_dir = args.dest
    dest = None
    if dest_dir:
        dest = Path(dest_dir).resolve()
        if dest.exists() and not dest.is_dir():
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
        set_launch_options(dest=dest, options=args.opts)

    if args.launch:
        launch_game(dest=dest, extra_opts=args.opts)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
