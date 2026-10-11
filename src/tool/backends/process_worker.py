"""Wait for the owner's process-tree binding before launching any command."""
import json
import subprocess
import sys


def main():
    command = json.loads(sys.stdin.buffer.readline(20001))
    result = subprocess.run(command, shell=isinstance(command, str), stdin=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
