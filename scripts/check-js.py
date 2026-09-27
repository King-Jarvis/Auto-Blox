#!/usr/bin/env python3
"""Compile every static .js with the browser that will run it."""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "zero2w_console", "static")


def browser():
    for name in ("chromium", "chromium-browser", "google-chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def main():
    exe = browser()
    if not exe:
        print("no chromium — nothing to check with")
        return 0

    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    profile = tempfile.mkdtemp(prefix="checkjs-")
    from jsc import compile_all          # noqa: E402  (kept beside this file)
    return compile_all(exe, STATIC, profile)


if __name__ == "__main__":
    raise SystemExit(main())
