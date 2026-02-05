import logging
import os
import sys
from pathlib import Path

from tc2_launcher.utils import DEV_INSTANCE

root_log = None


def setup_logger(log_folder: Path):
    global root_log

    log_file_path = log_folder / "tc2_launcher_log.txt"

    format_string = "%(asctime)s [%(levelname)-5.5s]  %(message)s"
    date_format = "%d-%b-%y %H:%M:%S"
    log_formatter = logging.Formatter(format_string, datefmt=date_format)

    root_log = logging.getLogger("tc2_launcher")

    # File handler
    file_handler = logging.FileHandler(log_file_path)
    file_handler.setFormatter(log_formatter)
    root_log.addHandler(file_handler)

    if DEV_INSTANCE or os.name != "nt":
        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(log_formatter)
        root_log.addHandler(console_handler)

    # Log level
    log_level = logging.INFO
    root_log.setLevel(log_level)


def critical(msg):
    if root_log is None:
        logging.critical(msg)
        return
    root_log.critical(msg)


def error(msg):
    if root_log is None:
        logging.error(msg)
        return
    root_log.error(msg)


def exception(msg):
    if root_log is None:
        logging.exception(msg)
        return
    root_log.exception(msg)


def warning(msg):
    if root_log is None:
        print(f"WARNING: {msg}")
        return
    root_log.warning(msg)


def info(msg):
    if root_log is None:
        print(f"INFO: {msg}")
        return
    root_log.info(msg)


def debug(msg):
    if root_log is None:
        logging.debug(msg)
        return
    root_log.debug(msg)
