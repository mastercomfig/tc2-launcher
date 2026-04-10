import sys

VERSION = (1, 0, 11)
VERSION_STR = ".".join(map(str, VERSION))
DEV_INSTANCE = not getattr(sys, "frozen", False)
