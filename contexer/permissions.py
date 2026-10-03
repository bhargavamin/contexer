"""Owner-only directory creation and tightening without following symlink targets."""
import os
import stat
from pathlib import Path


def ensure_private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("Refusing a symlink as a private data directory")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise OSError("Private data directory must belong to the current user")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            os.fchmod(descriptor, mode & ~0o077)
    finally:
        os.close(descriptor)
    return path
