import argparse
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet


def generate(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as key:
        key.write(Fernet.generate_key() + b"\n")
        key.flush()
        os.fsync(key.fileno())


def ensure(path: Path):
    try:
        generate(path)
    except FileExistsError:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_uid != os.geteuid()
            or metadata.st_size > 128
        ):
            raise ValueError(
                "Existing encryption key has unsafe ownership or permissions."
            ) from None
        Fernet(path.read_bytes().strip())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create a private encryption key without printing it."
    )
    parser.add_argument("--if-missing", action="store_true")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    (ensure if args.if_missing else generate)(args.path)
    print(f"Key ready at {args.path}. Grant read access only to the app's UID.")
