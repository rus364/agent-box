"""Standalone stdio client copied into a session, including older workspace images."""
import os
from pathlib import Path
import socket
import sys
import threading


def main():
    endpoint = Path(sys.argv[1])
    # Relative connect avoids AF_UNIX's short pathname limit for state roots.
    os.chdir(endpoint.parent)
    with socket.socket(socket.AF_UNIX) as connection:
        try:
            connection.connect(endpoint.name)
        except OSError as exc:
            print(f"agent-box browser unavailable: {exc}; restart this agent session", file=sys.stderr)
            return 1

        def send():
            try:
                while data := os.read(0, 65536):
                    connection.sendall(data)
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        threading.Thread(target=send, daemon=True).start()
        while data := connection.recv(65536):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
