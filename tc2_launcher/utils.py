import sys

VERSION = (0, 28, 0)
VERSION_STR = ".".join(map(str, VERSION))
DEV_INSTANCE = not getattr(sys, "frozen", False)
