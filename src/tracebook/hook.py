"""Silent stdlib-only capture. JSON parsing belongs to the daemon, not the hook."""

import os
import sys
import time
from _json import encode_basestring


def capture() -> None:
    forward, payload_arg = "", ""
    command: list[str] = []
    try:
        args = sys.argv[1:]
        agent, event = "unknown", ""
        index = 0
        while index < len(args):
            arg = args[index]
            if arg == "--forward-argv" and index + 1 < len(args):
                length = int(args[index + 1])
                if length < 1 or length > 256:
                    raise ValueError("Invalid forward command length")
                command = args[index + 2 : index + 2 + length]
                index += 2 + length
            elif arg in {"--agent", "--event", "--forward"} and index + 1 < len(args):
                value = args[index + 1]
                if arg == "--agent":
                    agent = value
                elif arg == "--event":
                    event = value
                else:
                    forward = value
                index += 2
            else:
                payload_arg = arg
                index += 1
        raw = payload_arg or (sys.stdin.read(65537) if not sys.stdin.isatty() else "{}")
        root = os.path.expanduser(os.environ.get("TRACEBOOK_HOME", "~/.tracebook"))
        spool = os.path.join(root, "spool")
        os.makedirs(spool, mode=0o700, exist_ok=True)
        stamp = time.time()
        # CPython's stdlib JSON string encoder avoids importing the decoder's
        # regex/enum dependencies on every agent tool invocation.
        line = (
            '{"agent":'
            + encode_basestring(agent)
            + ',"event":'
            + encode_basestring(event)
            + ',"payload_raw":'
            + encode_basestring(raw[:65536])
            + ',"truncated":'
            + ("true" if len(raw) > 65536 else "false")
            + ',"received_at":'
            + str(stamp)
            + ',"monotonic_ns":'
            + str(time.monotonic_ns())
            + ',"sequence":'
            + str(time.time_ns())
            + ',"pid":'
            + str(os.getpid())
            + "}\n"
        ).encode()
        filename = os.path.join(spool, str(os.getpid()) + "-" + str(int(stamp // 3600)) + ".jsonl")
        fd = os.open(
            filename, os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except BaseException:
        pass
    # Pre-existing notifications run even if spooling fails, with the original
    # argv payload and environment. Their output is deliberately muted.
    try:
        if forward:
            import json

            command = json.loads(forward)
        if isinstance(command, list) and command and all(isinstance(v, str) for v in command):
            if hasattr(os, "posix_spawnp"):
                os.posix_spawnp(
                    command[0],
                    [*command, payload_arg],
                    dict(os.environ),
                    setsid=True,
                    file_actions=[
                        (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0o600),
                        (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0o600),
                        (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0o600),
                    ],
                )
            else:
                import subprocess

                subprocess.Popen(
                    [*command, payload_arg],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
    except BaseException:
        pass


def main() -> None:
    capture()
    # All file descriptors were closed explicitly. No buffered output exists.
    # Avoid interpreter shutdown work on the agent's synchronous hook path.
    os._exit(0)


if __name__ == "__main__":
    main()
