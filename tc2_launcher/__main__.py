import argparse
import multiprocessing
from pathlib import Path
import sys

from tc2_launcher.gui import start_gui
from tc2_launcher.run import update_archive
from tc2_launcher.run import launch_game
from tc2_launcher.run import set_launch_options
from tc2_launcher.run import default_dest_dir

version = "0.1.0"


def main():
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

    if len(sys.argv) == 1:
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
