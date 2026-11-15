import os
import sys

from .daemon import main

try:
    code = main()
    sys.stdout.flush()
except BrokenPipeError:
    # The reader closed the pipe early (`omatuner status | head`); point
    # stdout at /dev/null so the interpreter's exit flush stays quiet.
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    code = 1
raise SystemExit(code)
