"""Install the provisioned Claude subscription bridge as a user launchd service."""

import argparse
import os
from pathlib import Path
import plistlib
import subprocess

from integrations.subscription.setup import REVISION

LABEL = "com.team.subscription"


def install(state: Path):
    state = state.expanduser().resolve()
    repo = Path(__file__).resolve().parents[2]
    transport = state / "subscription" / REVISION
    token = state / "subscription" / "token"
    if not token.is_file() or not (transport / "directsdk.py").is_file():
        raise SystemExit("Run integrations.subscription.setup for this state directory first")
    log = state / "subscription" / "bridge.log"
    log.touch(mode=0o600, exist_ok=True)
    config = {
        "Label": LABEL,
        "ProgramArguments": [
            str(repo / "scripts/run-in-hermes-env"), "python3", "-m",
            "integrations.subscription.server", "--plugin-dir", str(transport),
            "--token-file", str(token),
        ],
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", os.defpath),
            "TEAM_STATE": str(state),
            "HERMES_HOME": str(state / "hermes"),
            "HERMES_RUNTIME_DIR": str(state / "hermes-runtime"),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps(config))
    domain = f"gui/{os.getuid()}"
    loaded = subprocess.run(
        ["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True, check=False, timeout=30
    )
    if loaded.returncode == 0:
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=True, timeout=30)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, timeout=30)
    print(f"Installed {LABEL}; log: {log}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    install(parser.parse_args().state_dir)
