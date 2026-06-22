#!/usr/bin/env python3
"""Command-line note-taking tool.

Reference implementation shown to the LLM judge to anchor its grading.
The model's submission does not need to match this exactly — the judge
looks for whether each rubric criterion is satisfied, not verbatim equality.

Usage:
    python notes_cli.py add "Buy groceries"
    python notes_cli.py list
    python notes_cli.py search "groceries"
    python notes_cli.py delete 2
"""

import sys
from pathlib import Path

NOTES_FILE = Path("notes.txt")


def _load() -> list[str]:
    """Return all non-empty lines from notes.txt, or [] if the file is absent."""
    if not NOTES_FILE.exists():
        return []
    return [ln for ln in NOTES_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _save(notes: list[str]) -> None:
    NOTES_FILE.write_text("\n".join(notes) + ("\n" if notes else ""), encoding="utf-8")


def cmd_add(text: str) -> None:
    notes = _load()
    notes.append(text)
    _save(notes)
    print(f"Added: {text}")


def cmd_list() -> None:
    notes = _load()
    if not notes:
        print("(no notes)")
        return
    for i, note in enumerate(notes, 1):
        print(f"{i}. {note}")


def cmd_search(keyword: str) -> None:
    notes = _load()
    kw = keyword.lower()
    matches = [(i + 1, n) for i, n in enumerate(notes) if kw in n.lower()]
    if not matches:
        print("(no matches)")
        return
    for num, note in matches:
        print(f"{num}. {note}")


def cmd_delete(n: int) -> None:
    notes = _load()
    if n < 1 or n > len(notes):
        print(f"Error: note {n} does not exist (have {len(notes)} note(s)).")
        sys.exit(1)
    removed = notes.pop(n - 1)
    _save(notes)
    print(f"Deleted: {removed}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: notes_cli.py <add|list|search|delete> [args]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "add":
        if len(sys.argv) < 3:
            print("Usage: notes_cli.py add <text>")
            sys.exit(1)
        cmd_add(" ".join(sys.argv[2:]))

    elif cmd == "list":
        cmd_list()

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: notes_cli.py search <keyword>")
            sys.exit(1)
        cmd_search(" ".join(sys.argv[2:]))

    elif cmd == "delete":
        if len(sys.argv) < 3:
            print("Usage: notes_cli.py delete <n>")
            sys.exit(1)
        try:
            n = int(sys.argv[2])
        except ValueError:
            print("Error: <n> must be an integer.")
            sys.exit(1)
        cmd_delete(n)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
