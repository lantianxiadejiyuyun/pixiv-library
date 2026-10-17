"""Standalone AI Compose entry point; it never starts the library or MySQL."""
from pathlib import Path
import subprocess
import sys


def compose_command(arguments, root=None):
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    arguments = list(arguments)
    mode = arguments.pop(0) if arguments and arguments[0] in ("--intel", "--nvidia") else None
    command = ["docker", "compose", "--project-directory", str(root), "--env-file", str(root / "deploy/ai-node.env"),
               "-f", str(root / "deploy/compose.ai-node.yaml")]
    if mode:
        command.extend(["-f", str(root / ("deploy/compose.ai-node." + mode[2:] + ".yaml"))])
    return command + arguments


if __name__ == "__main__":
    if not sys.argv[1:]:
        raise SystemExit("Usage: python scripts/ai_node.py [--intel|--nvidia] up -d --build")
    raise SystemExit(subprocess.call(compose_command(sys.argv[1:])))
