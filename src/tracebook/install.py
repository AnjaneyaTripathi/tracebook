"""Conservative, idempotent configuration edits with exact-command ownership."""

from __future__ import annotations

import difflib
import json
import shlex
import sys
from pathlib import Path

import tomlkit

from tracebook import hook
from tracebook.adapters.base import Adapter
from tracebook.model import Diff, now, obj
from tracebook.platform import atomic_write

EVENTS = {
    "claude_code": [
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
        "PreCompact",
        "SubagentStart",
        "SubagentStop",
        "PermissionRequest",
    ],
    "gemini_cli": [
        "SessionStart",
        "BeforeAgent",
        "BeforeTool",
        "AfterTool",
        "AfterAgent",
        "PreCompress",
        "Notification",
    ],
}


def shim_command(agent: str, state_home: Path | None = None) -> list[str]:
    command = [sys.executable, "-S", str(Path(hook.__file__).resolve()), "--agent", agent]
    return [*command, "--home", str(state_home)] if state_home else command


def owned(command: object) -> bool:
    if isinstance(command, list):
        return (
            len(command) >= 5
            and command[2] == str(Path(hook.__file__).resolve())
            and command[3] == "--agent"
        )
    if isinstance(command, str):
        try:
            return owned(shlex.split(command))
        except ValueError:
            return False
    return False


def edit_hooks(adapter: Adapter, dry_run: bool = True, uninstall: bool = False) -> Diff:
    if adapter.id not in {*EVENTS, "codex", "antigravity"}:
        return Diff("", "This adapter has no command hooks.", False)
    path = adapter.hook_config_path()
    before = path.read_text() if path.exists() else ""
    if path.is_symlink():
        raise ValueError("Refusing to replace a symlinked agent configuration")
    if adapter.id == "antigravity":
        document_json = json.loads(before) if before.strip() else {}
        if not isinstance(document_json, dict):
            raise ValueError("Antigravity hooks must be a JSON object")
        if uninstall:
            document_json.pop("tracebook", None)
        else:
            document_json["tracebook"] = {
                "PostToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": shlex.join(
                                    [
                                        *shim_command(adapter.id, adapter.state_root),
                                        "--event",
                                        "PostToolUse",
                                    ]
                                ),
                            }
                        ],
                    }
                ],
                "SessionStart": [
                    {
                        "type": "command",
                        "command": shlex.join(
                            [
                                *shim_command(adapter.id, adapter.state_root),
                                "--event",
                                "SessionStart",
                            ]
                        ),
                    }
                ],
                "Stop": [
                    {
                        "type": "command",
                        "command": shlex.join(
                            [
                                *shim_command(adapter.id, adapter.state_root),
                                "--event",
                                "Stop",
                            ]
                        ),
                    }
                ],
            }
        after = (json.dumps(document_json, indent=2) + "\n") if document_json else ""
        if after:
            json.loads(after)
        if before and document_json and json.loads(before) == document_json:
            after = before
    elif adapter.id == "codex":
        document = tomlkit.parse(before)
        previous = document.get("notify")
        prior = (
            previous.unwrap() if previous is not None and hasattr(previous, "unwrap") else previous
        )
        if owned(prior):
            if uninstall:
                if isinstance(prior, list) and "--forward-argv" in prior:
                    position = prior.index("--forward-argv")
                    length = int(prior[position + 1])
                    document["notify"] = prior[position + 2 : position + 2 + length]
                elif isinstance(prior, list) and "--forward" in prior:
                    document["notify"] = json.loads(prior[prior.index("--forward") + 1])
                else:
                    del document["notify"]
        elif not uninstall:
            if prior is not None and (
                not isinstance(prior, list) or not all(isinstance(v, str) for v in prior)
            ):
                raise ValueError("Existing notify is not an argv array")
            command = shim_command(adapter.id, adapter.state_root)
            if prior:
                command += ["--forward-argv", str(len(prior)), *prior]
            document["notify"] = command
        after = tomlkit.dumps(document)
        tomlkit.parse(after)
    else:
        document_json = json.loads(before) if before.strip() else {}
        if not isinstance(document_json, dict):
            raise ValueError("Agent settings must be a JSON object")
        hooks = document_json.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("Existing hooks must be an object")
        for event in EVENTS[adapter.id]:
            groups = hooks.get(event, [])
            if not isinstance(groups, list):
                raise ValueError(f"Existing {event} hooks must be an array")
            found = False
            rebuilt = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
                    raise ValueError("Unrecognized hook group; refusing edit")
                entries = []
                for entry in group.get("hooks", []):
                    if owned(obj(entry).get("command")):
                        found = True
                        if uninstall:
                            continue
                    entries.append(entry)
                if entries or not group.get("hooks"):
                    rebuilt.append({**group, "hooks": entries})
            if not uninstall and not found:
                rebuilt.append(
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": shlex.join(
                                    [
                                        *shim_command(adapter.id, adapter.state_root),
                                        "--event",
                                        event,
                                    ]
                                ),
                            }
                        ],
                    }
                )
            if rebuilt:
                hooks[event] = rebuilt
            elif event in hooks:
                del hooks[event]
        if not hooks:
            document_json.pop("hooks", None)
        after = json.dumps(document_json, indent=2) + "\n"
        json.loads(after)
        # Formatting alone must not create a backup or mutate user settings.
        if before and json.loads(before) == document_json:
            after = before
    changed = before != after
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    if changed and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        if before:
            suffix = now().replace(":", "-")
            atomic_write(path.with_name(path.name + ".tracebook-" + suffix + ".bak"), before)
        # Check for another writer between read and replace.
        if path.exists() and path.read_text() != before:
            raise ValueError("Agent settings changed during edit; retry")
        atomic_write(path, after)
    return Diff(str(path), diff, changed)
