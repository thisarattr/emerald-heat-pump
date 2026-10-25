import argparse
import os
from pathlib import Path

from cryptography.fernet import Fernet


def generate(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as key:
        key.write(Fernet.generate_key() + b"\n")
        key.flush()
        os.fsync(key.fileno())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create a private encryption key without printing it."
    )
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    generate(args.path)
    print(f"Key created at {args.path}. Grant read access only to the app's UID.")
