"""Dependency-free, silent, fail-open capture shim. Runnable with python -S."""
import json
import os
import sys
import time


def _limit(value: object, depth: int = 0) -> object:
    if depth > 20:
        return {"truncated": True}
    if isinstance(value, str) and len(value) > 16384:
        return {"text": value[:16384], "truncated": True}
    if isinstance(value, dict):
        return {k: _limit(v, depth + 1) for k, v in list(value.items())[:256]}
    if isinstance(value, list):
        return [_limit(v, depth + 1) for v in value[:256]]
    return value


def main() -> None:
    try:
        args = sys.argv[1:]
        agent, event, forward, payload_arg = "unknown", "", "", ""
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in {"--agent", "--event", "--forward"} and index + 1 < len(args):
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
        # Codex appends JSON in argv. Other hooks provide stdin.
        raw = payload_arg or (sys.stdin.read(1048577) if not sys.stdin.isatty() else "{}")
        try:
            payload = json.loads(raw[:1048576])
        except (ValueError, TypeError):
            payload = {"unparsed": raw[:16384], "truncated": len(raw) > 16384}
        root = os.path.expanduser(os.environ.get("TRACEBOOK_HOME", "~/.tracebook"))
        spool = os.path.join(root, "spool")
        os.makedirs(spool, mode=0o700, exist_ok=True)
        stamp = time.time()
        envelope = {"agent": agent, "event": event, "payload": _limit(payload), "received_at": stamp, "monotonic_ns": time.monotonic_ns(), "sequence": time.time_ns(), "pid": os.getpid()}
        line = json.dumps(envelope, separators=(",", ":")).encode() + b"\n"
        if len(line) > 262144:
            envelope["payload"] = {"truncated": True, "prefix": raw[:16384]}
            line = json.dumps(envelope, separators=(",", ":")).encode() + b"\n"
        filename = os.path.join(spool, str(os.getpid()) + "-" + str(int(stamp // 3600)) + ".jsonl")
        fd = os.open(filename, os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except BaseException:
        pass
    # User notification must still run if spooling fails. Never wait for it.
    try:
        if forward:
            import subprocess
            command = json.loads(forward)
            if isinstance(command, list) and command and all(isinstance(v, str) for v in command):
                subprocess.Popen([*command, payload_arg], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except BaseException:
        pass


if __name__ == "__main__":
    main()
