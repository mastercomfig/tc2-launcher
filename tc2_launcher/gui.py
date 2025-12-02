import multiprocessing
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import webview

from tc2_launcher.run import (
    DEV_INSTANCE,
    change_install_folder,
    get_launch_options,
    launch_game,
    open_install_folder,
    set_launch_options,
    update_archive,
    wait_game_exit,
    wait_game_running,
)


def get_window(idx: int = 0):
    return webview.windows[idx]


def get_entrypoint():
    def exists(path):
        path = (Path(__file__).parent / path).resolve()
        if path.exists():
            return path

    queries = ["../gui", "../Resources/gui", "./gui"]

    for query in queries:
        result = exists(query)
        if result:
            return result

    raise Exception("No gui directory found")


class Api:
    def launch_game(self):
        launch_game()
        check_launch_game()

    def set_launch_options(self, options):
        if options and isinstance(options, str):
            options = options.split()
        else:
            options = None
        set_launch_options(extra_options=options)

    def open_install_folder(self):
        open_install_folder()

    def check_for_updates(self):
        res = update_archive()
        get_window().evaluate_js(f"archiveReady({res});")

    def move_install_folder(self):
        try:
            result = get_window().create_file_dialog(webview.FileDialog.FOLDER)
            if not result:
                return
            path = result[0]
            new_game_dir = Path(path).resolve()
            change_install_folder(new_game_dir)
        except Exception as e:
            print(f"ERROR: Invalid path: {e}")
            return


def update_and_notify(window):
    res = update_archive()
    window.evaluate_js(f"archiveReady({res});")


def on_game_exit():
    window = get_window()
    window.evaluate_js("setLaunchState(0);")
    update_and_notify(window)


def check_launch_game(time_limit: float = 0):
    pid = wait_game_running(time_limit)
    has_pid = pid is not None
    res = 2 if has_pid else 0
    get_window().evaluate_js(f"setLaunchState({res});")
    if has_pid:
        wait_game_exit(pid, on_game_exit)
        return True
    else:
        return False


def check_queue():
    while True:
        if not current_queue:
            return
        cmd = current_queue.get()
        if cmd == "close":
            close_gui()
            return


def on_loaded(window):
    if current_queue:
        threading.Thread(target=check_queue).start()
    if current_entry != "index":
        return
    if not check_launch_game(-1):
        update_and_notify(window)


entry_parent = get_entrypoint()
current_entry: str | None = None
current_queue: Optional[multiprocessing.Queue] = None


def close_gui():
    window = get_window()
    window.destroy()


def start_gui_separate(entry_name: str = "index", **kwargs):
    q = multiprocessing.Queue()
    p = multiprocessing.Process(
        target=_start_gui_private, args=(entry_name, q), kwargs=kwargs
    )
    p.start()
    return p, q


def start_gui(entry_name: str = "index", **kwargs):
    _start_gui_private(entry_name, **kwargs)


def _start_gui_private(
    entry_name: str = "index", queue: Optional[multiprocessing.Queue] = None, **kwargs
):
    global current_entry
    global current_queue
    extra_options = get_launch_options()
    extra_options_str = " ".join(extra_options)
    entry = str(entry_parent / f"{entry_name}.html")
    current_entry = entry_name
    current_queue = queue
    width = 800
    height = 600
    min_size = (640, 360)
    if entry_name == "update":
        width = 400
        height = 533
        min_size = (400, 533)
    window = webview.create_window(
        "Team Comtress Launcher",
        entry,
        js_api=Api(),
        min_size=min_size,
        width=width,
        height=height,
        background_color="#212121",
        **kwargs,
    )
    if window:
        window.state.opts = extra_options_str
        window.events.loaded += lambda: on_loaded(window)
        try:
            webview.start(icon=str(entry_parent / "favicon.ico"), debug=DEV_INSTANCE)
        except Exception as e:
            if os.name == "posix" and sys.platform != "darwin":
                subprocess.run(
                    ["/usr/bin/notify-send", "--icon=error", f"TC2 Launcher Error: {e}"]
                )
            raise e
    else:
        print("Failed to create webview window.")
