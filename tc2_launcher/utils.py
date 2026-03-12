import sys

VERSION = (0, 26, 0)
VERSION_STR = ".".join(map(str, VERSION))
DEV_INSTANCE = not getattr(sys, "frozen", False)
