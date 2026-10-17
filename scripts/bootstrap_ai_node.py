"""Generate a standalone AI node secret; never overwrite or print it."""
import argparse
from pathlib import Path
import secrets
import uuid


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=root / "deploy" / "ai-node.env")
    args = parser.parse_args()
    template = (root / "deploy" / "ai-node.env.example").read_text(encoding="utf-8")
    template = template.replace("QIYE_CONTROLLER_KEY=\n", "QIYE_CONTROLLER_KEY=" + secrets.token_urlsafe(32) + "\n")
    template = template.replace("QIYE_NODE_ID=\n", "QIYE_NODE_ID=" + str(uuid.uuid4()) + "\n")
    try:
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(template)
    except FileExistsError:
        raise SystemExit("AI node configuration already exists; retained without changes") from None
    try:
        args.output.chmod(0o600)
    except OSError:
        pass
    print("AI node configuration created. Use deploy/ai-node.env to configure the controller in the library.")


if __name__ == "__main__":
    main()
