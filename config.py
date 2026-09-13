import json
import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
EXAMPLE_PATH = BASE_DIR / "config.example.json"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

if not CONFIG_PATH.exists() and EXAMPLE_PATH.exists():
    shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)

_env_path = BASE_DIR / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

CONFIG["token"] = (
    os.environ.get("DISCORD_TOKEN")
    or os.environ.get("DISCORD_BOT_TOKEN")
    or CONFIG.get("token", "")
)

TOKEN = CONFIG["token"]
GUILD_ID = CONFIG.get("guild_id", 0)
CHANNEL_ID = CONFIG.get("channel_id", 0)
PROXY_URL = os.environ.get("DISCORD_PROXY", "")

RCON = CONFIG.get("rcon", {})
RCON["token"] = os.environ.get("RCON_TOKEN") or RCON.get("token", "")