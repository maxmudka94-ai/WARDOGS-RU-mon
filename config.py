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

# ---------- панель управления ----------

PANEL = CONFIG.get("panel", {}) or {}
PANEL_ENABLED = bool(
    (os.environ.get("PANEL_ENABLED", "") or str(PANEL.get("enabled", "true"))).lower()
    in ("1", "true", "yes", "on")
)
PANEL_HOST = os.environ.get("PANEL_HOST") or PANEL.get("host") or "127.0.0.1"
PANEL_PORT = int(os.environ.get("PANEL_PORT") or PANEL.get("port") or 8200)
PANEL_PASSWORD = (
    os.environ.get("PANEL_PASSWORD") or PANEL.get("password") or ""
)
PANEL_PUBLIC_URL = os.environ.get("PANEL_PUBLIC_URL") or PANEL.get("public_url") or ""
PANEL_REFRESH = float(os.environ.get("PANEL_REFRESH") or PANEL.get("refresh_seconds") or 5)

# ---------- мультисервер ----------

SERVERS = []
_raw_servers = CONFIG.get("servers") or []
if not _raw_servers and RCON.get("host"):
    _raw_servers = [RCON]

for i, s in enumerate(_raw_servers, 1):
    sid = str((s.get("id") or "").strip() or f"srv{i}")
    srv = dict(s)
    srv["id"] = sid
    srv["name"] = srv.get("name") or srv.get("serverName") or f"WARDOGS #{i}"
    env_token = os.environ.get(f"RCON_TOKEN_{sid.upper().replace('-', '_')}")
    srv["token"] = env_token or srv.get("token") or os.environ.get("RCON_TOKEN") or ""
    srv["timeout"] = int(srv.get("timeout") or 45)
    srv["connect_url"] = srv.get("connect_url") or ""
    SERVERS.append(srv)