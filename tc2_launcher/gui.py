import webview

from pathlib import Path

from tc2_launcher.run import (
    get_launch_options,
    open_install_folder,
    update_archive,
    launch_game,
    set_launch_options,
)


class Api:
    def launch_game(self):
        launch_game()

    def set_launch_options(self, options):
        if options and isinstance(options, str):
            options = options.split()
        else:
            options = None
        set_launch_options(options=options)

    def open_install_folder(self):
        open_install_folder()

    def check_for_updates(self):
        res = update_archive()
        webview.windows[0].evaluate_js(f"archiveReady({res});")


def get_entrypoint():
    def exists(path):
        path = (Path(__file__).parent / path).resolve()
        if path.exists():
            return path

    queries = ["../gui/index.html", "../Resources/gui/index.html", "./gui/index.html"]

    for query in queries:
        result = exists(query)
        if result:
            return result

    raise Exception("No index.html found")


def on_loaded(window):
    res = update_archive()
    window.evaluate_js(f"archiveReady({res});")


entry_path = get_entrypoint()
entry = str(entry_path)
entry_parent = entry_path.parent


def start_gui():
    opts = get_launch_options()
    opt_str = " ".join(opts)
    window = webview.create_window(
        "Team Comtress Launcher", entry, js_api=Api(), min_size=(640, 360)
    )
    if window:
        window.state.opts = opt_str
        window.events.loaded += lambda: on_loaded(window)
        webview.start(icon=str(entry_parent / "favicon.ico"))
    else:
        print("Failed to create webview window.")
