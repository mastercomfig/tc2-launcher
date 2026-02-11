import asyncio
import multiprocessing
import os
import sys
import threading
import traceback
from _thread import interrupt_main
from pathlib import Path
from time import sleep
from timeit import default_timer as timer
from typing import Optional

import webview

from tc2_launcher import logger
from tc2_launcher.run import (
    change_install_folder,
    get_launch_options,
    get_prerelease,
    launch_game,
    open_install_folder,
    run_open,
    set_launch_options,
    set_prerelease,
    update_archive,
    wait_game_exit,
    wait_game_running,
)
from tc2_launcher.utils import DEV_INSTANCE, VERSION


def get_window(idx: int = 0):
    return webview.windows[idx]


eval_queue: list[str] = []


def send_eval(script: str):
    global using_fallback
    if using_fallback:
        eval_queue.append(script)
    else:
        get_window().evaluate_js(script)


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

    def set_prerelease(self, prerelease: str):
        if isinstance(prerelease, str):
            set_prerelease(prerelease=prerelease)

    def check_for_updates(self):
        res = update_archive()
        send_eval(f"archiveReady({res});")

    def open_install_folder(self):
        open_install_folder()

    def move_install_folder(self):
        try:
            result = get_window().create_file_dialog(webview.FileDialog.FOLDER)
            if not result:
                return
            path = result[0]
            new_game_dir = Path(path).resolve()
            change_install_folder(new_game_dir)
        except Exception as e:
            logger.error(f"Invalid path: {e}")
            return


def update_and_notify():
    res = update_archive()
    send_eval(f"archiveReady({res});")


def on_game_exit():
    send_eval("setLaunchState(0);")
    update_and_notify()


def check_launch_game(time_limit: float = 0):
    pid = wait_game_running(time_limit)
    has_pid = pid is not None
    res = 2 if has_pid else 0
    send_eval(f"setLaunchState({res});")
    if has_pid:
        wait_game_exit(pid, on_game_exit)
        return True
    else:
        return False


queue_thread = None


def check_queue():
    global queue_thread
    while True:
        if sys.is_finalizing():
            return
        if current_queue is None:
            sleep(1)
            continue
        cmd = current_queue.get()
        if cmd == "close":
            queue_thread = None
            close_gui()
            return


def on_loaded(window):
    global queue_thread
    if current_queue is not None and queue_thread is None:
        queue_thread = threading.Thread(target=check_queue)
        queue_thread.start()
    if current_entry != "index":
        return
    if not check_launch_game(-1):
        update_and_notify()


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


last_eval_time = None

fallback_keep_alive_thread = None


def fallback_keep_alive():
    global last_eval_time
    watch_start_time = timer()
    while True:
        sleep(10 if last_eval_time is None else 2)
        if sys.is_finalizing():
            return
        keepalive_time = 10
        if last_eval_time is not None:
            watch_start_time = last_eval_time
            keepalive_time = 2
        if timer() - watch_start_time >= keepalive_time:
            if os.name == "nt":
                interrupt_main()
            else:
                os.kill(os.getpid(), 2)
            return


def start_fallback_keep_alive():
    global fallback_keep_alive_thread
    if fallback_keep_alive_thread is not None:
        return
    fallback_keep_alive_thread = threading.Thread(target=fallback_keep_alive)
    fallback_keep_alive_thread.start()


using_fallback = False


async def start_fallback_gui(entry: str, extra_options: str, branch: str):
    global last_eval_time
    # start a local http server and return the address
    try:
        import aiohttp.web

        api = Api()

        app = aiohttp.web.Application()

        app.add_routes([aiohttp.web.static("/", entry_parent)])

        async def index(r: aiohttp.web.Request):
            return aiohttp.web.FileResponse(entry)

        app.add_routes([aiohttp.web.get("/entry", index)])

        async def state_handler(r: aiohttp.web.Request):
            on_loaded(None)
            return aiohttp.web.json_response(
                {
                    "opts": extra_options,
                    "branch": branch,
                    "version": VERSION,
                }
            )

        app.add_routes([aiohttp.web.post("/api/state", state_handler)])

        async def eval_handler(r: aiohttp.web.Request):
            global last_eval_time
            last_eval_time = timer()
            to_send = []
            while eval_queue:
                to_send.append(eval_queue.pop())
            return aiohttp.web.json_response(to_send)

        app.add_routes([aiohttp.web.post("/api/eval", eval_handler)])

        class ApiCallbackHandler:
            def __init__(self, func, param=False):
                self.func = func
                self.param = param

            async def __call__(self, r: aiohttp.web.Request):
                if self.param:
                    self.func(await r.text())
                else:
                    self.func()
                return aiohttp.web.Response()

        class ApiCallbackWithParamHandler(ApiCallbackHandler):
            def __init__(self, func):
                super().__init__(func, param=True)

        app.add_routes(
            [
                aiohttp.web.post(
                    "/api/launch_game",
                    ApiCallbackHandler(api.launch_game),
                )
            ]
        )
        app.add_routes(
            [
                aiohttp.web.post(
                    "/api/set_launch_options",
                    ApiCallbackWithParamHandler(api.set_launch_options),
                )
            ]
        )
        app.add_routes(
            [
                aiohttp.web.post(
                    "/api/set_prerelease",
                    ApiCallbackWithParamHandler(api.set_prerelease),
                )
            ]
        )
        app.add_routes(
            [
                aiohttp.web.post(
                    "/api/check_for_updates",
                    ApiCallbackHandler(api.check_for_updates),
                )
            ]
        )
        app.add_routes(
            [
                aiohttp.web.post(
                    "/api/open_install_folder",
                    ApiCallbackHandler(api.open_install_folder),
                )
            ]
        )
        app.add_routes(
            [
                aiohttp.web.post(
                    "/api/move_install_folder",
                    ApiCallbackHandler(api.move_install_folder),
                )
            ]
        )

        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, "localhost", 48564)
        await site.start()
        address = "http://localhost:48564/entry"
        try:
            run_open(address)
        except Exception as e:
            logger.error(f"Could not open fallback GUI: {e}")
            pass
        logger.info(f"Fallback GUI available at {address}")
        start_fallback_keep_alive()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await runner.cleanup()
    except Exception as e:
        logger.error(f"Could not start fallback GUI: {e}")


def fallback_gui_main(entry: str, extra_options: str, branch: str):
    global using_fallback
    using_fallback = True
    asyncio.run(start_fallback_gui(entry, extra_options, branch))


def _start_gui_private(
    entry_name: str = "index", queue: Optional[multiprocessing.Queue] = None, **kwargs
):
    global current_entry
    global current_queue
    extra_options = get_launch_options()
    extra_options_str = " ".join(extra_options)
    branch = get_prerelease()
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
    if not window:
        logger.error("Failed to create webview window.")
    if window:
        window.state.opts = extra_options_str
        window.state.branch = branch
        window.state.version = VERSION
        window.events.loaded += lambda: on_loaded(window)
        try:
            webview.start(icon=str(entry_parent / "favicon.ico"), debug=DEV_INSTANCE)
        except Exception as e:
            logger.error("Failed to start webview window.")
            logger.error(traceback.format_exc())
            window = None
    if not window:
        fallback_gui_main(entry, extra_options_str, branch)
