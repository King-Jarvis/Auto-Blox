"""Where the console keeps what it stores: ~/.config/auto-blox.

It used to be ~/.config/zero2w-console, after the project's first name, which
read as the name of a board. `migrate()` moves an existing folder across the
first time the new console starts, and leaves the old name as a link to the new
one, so a script or a habit that still says zero2w-console finds everything.
The token, the flows, the enrolled devices and the TLS key all move together,
in one rename, so boards already enrolled keep working.
"""
import os

CONFIG_HOME = os.path.join(os.path.expanduser("~"), ".config")
CONFIG_DIR = os.path.join(CONFIG_HOME, "auto-blox")
OLD_CONFIG_DIR = os.path.join(CONFIG_HOME, "zero2w-console")


def migrate(new=CONFIG_DIR, old=OLD_CONFIG_DIR):
    """Move the old folder to the new name, once. Returns what it did, as a
    sentence for the log, or None when there was nothing to do."""
    if os.path.islink(old) or not os.path.isdir(old):
        return None                     # nothing there, or already moved
    if os.path.exists(new):
        return ("%s and %s both exist; using %s and leaving the old one as it "
                "is" % (new, old, new))
    os.rename(old, new)                 # one filesystem, so atomic
    try:
        os.symlink(os.path.basename(new), old)
    except OSError:
        pass                            # the move is what matters
    return "moved the console's settings from %s to %s" % (old, new)


def token_file():
    """The token, for the scripts that read it: the new place, or the old one
    when this console has not yet started since the move."""
    for base in (CONFIG_DIR, OLD_CONFIG_DIR):
        path = os.path.join(base, "token")
        if os.path.exists(path):
            return path
    return os.path.join(CONFIG_DIR, "token")
