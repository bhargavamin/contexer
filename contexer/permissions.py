"""Owner-only directory creation and tightening without following symlink targets."""
import os
import stat
from pathlib import Path


def ensure_private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("Refusing a symlink as a private data directory")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise OSError("Private data directory must belong to the current user")
    if not stat.S_IMODE(info.st_mode) & 0o077:
        return path
    # Metadata-only opens preserve write/search-only owner modes such as 0300.
    access = getattr(os, "O_PATH", getattr(os, "O_SEARCH", os.O_RDONLY))
    descriptor = os.open(path, access | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise OSError("Private data directory must belong to the current user")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            if access == getattr(os, "O_PATH", None):
                # Linux O_PATH descriptors cannot be fchmod'ed; this proc link names the
                # verified open inode even if another process replaces the original path.
                os.chmod(f"/proc/self/fd/{descriptor}", mode & ~0o077)
            else:
                os.fchmod(descriptor, mode & ~0o077)
    finally:
        os.close(descriptor)
    return path
