"""Fetch the reviewed transport and provision only a local gateway token."""

import argparse
import os
from pathlib import Path
import secrets
import shutil
from urllib.request import urlopen

REVISION = "20048e6e2c1c48d6023e73ae6951595f58352b20"
SOURCE = f"https://raw.githubusercontent.com/NousResearch/hermes-official-plugins/{REVISION}/claude-subscription-directsdk"
FILES = ("directsdk.py", "admission.py", "directsdk_setup.py", "model_catalog.py", "inert_mcp.py", "LICENSE")


def install_profile(home: Path):
    profile = home / "plugins" / "model-providers" / "team-subscription"
    profile.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("hermes_provider.py"), profile / "__init__.py")
    (profile / "plugin.yaml").write_text(
        "name: team-subscription\nversion: 0.1.0\nkind: model-provider\n"
        "description: Claude subscription via Team's LiteLLM and Portkey proxies\n"
    )


def install(state: Path):
    transport = state / "subscription" / REVISION
    for name in FILES:
        with urlopen(f"{SOURCE}/{name}", timeout=60) as response:
            data = response.read()
        transport.mkdir(parents=True, exist_ok=True)
        (transport / name).write_bytes(data)
    env_file = state / "portkey" / ".env"
    existing = env_file.read_text() if env_file.exists() else ""
    tokens = [line.partition("=")[2].strip() for line in existing.splitlines()
              if line.startswith("SUBSCRIPTION_TOKEN=")]
    token = tokens[-1] if tokens else secrets.token_urlsafe(32)
    if not token:
        raise ValueError("Existing SUBSCRIPTION_TOKEN is empty")
    if not tokens:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        with open(env_file, "a", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
            output.write(("\n" if existing and not existing.endswith("\n") else "")
                         + f"SUBSCRIPTION_TOKEN={token}\n")
    env_file.chmod(0o600)
    token_file = state / "subscription" / "token"
    with open(token_file, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        output.write(token + "\n")
    token_file.chmod(0o600)
    install_profile(state / "hermes")
    print(f"Transport: {transport}\nLocal token file: {token_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    install(parser.parse_args().state_dir)
