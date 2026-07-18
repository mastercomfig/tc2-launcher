import sys

VERSION = (1, 1, 3)
VERSION_STR = ".".join(map(str, VERSION))
DEV_INSTANCE = not getattr(sys, "frozen", False)
