import sys

DEV_INSTANCE = not getattr(sys, "frozen", False)
