import asyncio
import json
import multiprocessing
import os
import random
import socket
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
from tc2_launcher.env import is_qt_environment
from tc2_launcher.run import (
    change_install_folder,
    get_game_dir,
    get_launch_options,
    get_prerelease,
    launch_game,
    open_install_folder,
    run_async_task,
    run_open,
    set_launch_options,
    set_prerelease,
    subscribe_game_version_change,
    update_archive,
    wait_game_exit,
    wait_game_running,
)
from tc2_launcher.utils import DEV_INSTANCE, VERSION_STR

using_fallback = False

state = {
    "opts": None,
    "branch": None,
    "version": VERSION_STR,
    "game_version": "",
    "game_version_digest": "",
}


def get_window(idx: int = 0):
    return webview.windows[idx]


eval_queue: list[str] = []


def evaluate_js_thread(script: str):
    get_window().evaluate_js(script)


def send_eval(script: str):
    if using_fallback:
        eval_queue.append(script)
    else:
        threading.Thread(target=evaluate_js_thread, args=(script,)).start()


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
    async def launch_game_async(self):
        err, _ = launch_game()
        if err:
            send_eval(f"showErrorModal('Error', {json.dumps(err)});")
            send_eval("setLaunchState(0);")
        else:
            await check_launch_game()

    def launch_game(self):
        run_async_task(self.launch_game_async)

    def set_launch_options(self, options):
        if options and isinstance(options, str):
            options = options.split()
        else:
            options = None
        set_launch_options(extra_options=options)
        opts_str = " ".join(options) if options else ""
        if using_fallback:
            state["opts"] = opts_str
            send_eval("requestStateUpdate();")
        else:
            get_window().state.opts = opts_str

    def set_prerelease(self, prerelease: str):
        if isinstance(prerelease, str):
            set_prerelease(prerelease=prerelease)
        else:
            prerelease = ""
        if using_fallback:
            state["branch"] = prerelease
            send_eval("requestStateUpdate();")
        else:
            get_window().state.branch = prerelease

    async def check_for_updates_async(self):
        res = await update_archive()
        send_eval(f"archiveReady({res});")

    def check_for_updates(self):
        run_async_task(self.check_for_updates_async)

    def open_install_folder(self):
        open_install_folder()

    def move_install_folder(self, path: str | None = None):
        try:
            if path is None:
                if using_fallback:
                    return
                result = get_window().create_file_dialog(webview.FileDialog.FOLDER)
                if not result:
                    return
                path = result[0]
            new_game_dir = Path(path).resolve()
            change_install_folder(new_game_dir)
        except Exception as e:
            logger.error(f"Invalid path: {e}")
            return


def find_available_port(start_port: int, max_attempts: int = 100) -> int:
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except socket.error:
                continue
    return 0


async def update_and_notify():
    res = await update_archive()
    send_eval(f"archiveReady({res});")


def on_game_exit():
    send_eval("setLaunchState(0);")
    asyncio.run(update_and_notify())


async def check_launch_game(time_limit: float = 0):
    pid = await wait_game_running(time_limit)
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


has_init = False


def on_loaded():
    global has_init
    global queue_thread
    if current_queue is not None and queue_thread is None:
        queue_thread = threading.Thread(target=check_queue)
        queue_thread.start()
    if current_entry != "index":
        return

    async def task():
        if not await check_launch_game(-1):
            await update_and_notify()

    run_async_task(task)

    if has_init and not using_fallback:
        # if we refresh the page, we need a little kick to the state since the page reverted back to initial state
        window = get_window()
        for k, v in window.state.items():
            window.state.__setattr__(k, "")
            window.state.__setattr__(k, v)
    has_init = True


entry_parent = get_entrypoint()
current_entry: str | None = None
current_queue: Optional[multiprocessing.Queue] = None


def close_gui():
    if using_fallback:
        return
    window = get_window()
    if not window:
        return
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
        keepalive_time = 10 if last_eval_time is None else 5
        sleep(keepalive_time + random.uniform(-0.5, 0.5))
        if sys.is_finalizing():
            return
        if last_eval_time is not None:
            watch_start_time = last_eval_time
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


async def start_fallback_gui(entry: str, extra_options: str, branch: str):
    global last_eval_time
    # init state
    state["opts"] = extra_options
    state["branch"] = branch
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
            update = await r.text()
            if not update:
                on_loaded()
            return aiohttp.web.json_response(state)

        app.add_routes([aiohttp.web.post("/api/state", state_handler)])

        async def eval_handler(r: aiohttp.web.Request):
            global last_eval_time
            last_eval_time = timer()
            to_send = []
            while eval_queue:
                to_send.append(eval_queue.pop())
            return aiohttp.web.json_response(to_send)

        app.add_routes([aiohttp.web.post("/api/eval", eval_handler)])

        async def move_install_folder_handler(r: aiohttp.web.Request):
            path = await r.text()
            loop = asyncio.get_event_loop()
            if path:
                await loop.run_in_executor(None, api.move_install_folder, path)
            else:
                await loop.run_in_executor(None, api.move_install_folder)
            return aiohttp.web.Response()

        app.add_routes(
            [aiohttp.web.post("/api/move_install_folder", move_install_folder_handler)]
        )

        async def get_install_folder_handler(r: aiohttp.web.Request):
            path = str(get_game_dir())
            return aiohttp.web.json_response({"path": path})

        app.add_routes(
            [aiohttp.web.post("/api/get_install_folder", get_install_folder_handler)]
        )

        class ApiCallbackHandler:
            def __init__(self, func, param=False):
                self.func = func
                self.param = param

            async def __call__(self, r: aiohttp.web.Request):
                if self.param:
                    arg = await r.text()
                    if asyncio.iscoroutinefunction(self.func):
                        await self.func(arg)
                    else:
                        self.func(arg)
                else:
                    if asyncio.iscoroutinefunction(self.func):
                        await self.func()
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
                    ApiCallbackHandler(api.launch_game_async),
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
                    ApiCallbackHandler(api.check_for_updates_async),
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

        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        port = find_available_port(48564)
        site = aiohttp.web.TCPSite(runner, "localhost", port)
        await site.start()
        address = f"http://localhost:{port}/entry"
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
        logger.info("Exiting fallback GUI")
        remaining_threads = ""
        for thread in threading.enumerate():
            if thread.name != "MainThread" and not thread.name.startswith("asyncio_"):
                remaining_threads += f"\t{thread.name}\n"
        if remaining_threads:
            remaining_threads = "Waiting on remaining threads:\n" + remaining_threads
            logger.warning(remaining_threads)
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
    global using_fallback

    extra_options = get_launch_options()
    extra_options_str = " ".join(extra_options)

    branch = get_prerelease()

    entry = str(entry_parent / f"{entry_name}.html")

    current_entry = entry_name
    current_queue = queue

    supported_os = os.name == "nt"
    if supported_os:
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
    else:
        window = None
        using_fallback = True

    def subscribe_game_version():
        def on_game_version_change(tag, digest):
            game_version = tag if tag is not None else ""
            game_version_digest = digest if digest is not None else ""
            if using_fallback:
                state["game_version"] = game_version
                state["game_version_digest"] = game_version_digest
                send_eval("requestStateUpdate();")
            else:
                window.state.game_version = game_version
                window.state.game_version_digest = game_version_digest

        subscribe_game_version_change(on_game_version_change)

    if window:
        window.state.opts = extra_options_str
        window.state.branch = branch
        window.state.version = VERSION_STR
        # state needs empty init
        window.state.game_version = ""
        window.state.game_version_digest = ""
        window.events.loaded += on_loaded

        # then subscribe
        subscribe_game_version()

        try:
            gui = None
            if os.name == "posix":
                gui_pref = os.getenv("PYWEBVIEW_GUI")
                if not gui_pref or gui_pref == "qt":
                    gui = "gtk"
            webview.start(
                icon=str(entry_parent / "favicon.ico"),
                gui=gui,
                debug=DEV_INSTANCE,
                http_port=find_available_port(48564),
            )
        except Exception:
            logger.error("Failed to start webview window.")
            logger.error(traceback.format_exc())
            window = None
    else:
        using_fallback = True
        subscribe_game_version()
    if not window:
        fallback_gui_main(entry, extra_options_str, branch)
