"""Run Compose with deployment files and a stable repository project directory."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def compose_command(*arguments, intel=False, root=None):
    project = Path(root or ROOT).resolve()
    deployment = project / "deploy"
    command = ["docker", "compose", "--project-directory", str(project),
               "--env-file", str(deployment / ".env"), "-f", str(deployment / "compose.yaml")]
    if intel:
        command.extend(["-f", str(deployment / "compose.intel.yaml")])
    return command + list(arguments)


def main():
    arguments = sys.argv[1:]
    intel = bool(arguments and arguments[0] == "--intel")
    if intel:
        arguments = arguments[1:]
    if not arguments:
        print("Usage: python scripts/compose.py [--intel] <compose arguments>")
        return 2
    return subprocess.call(compose_command(*arguments, intel=intel), cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
