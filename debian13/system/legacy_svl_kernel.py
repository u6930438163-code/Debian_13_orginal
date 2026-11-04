# Vendored compatibility source: mmitek515-netizen/SVL_System/svl_kernel.py
# Original source blob SHA: e2f1b867000c7bddcc80dd4d56b5cccedcb2d0e1
# Legacy deterministic state engine; not a bootable OS, AI worker or root shell.
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STORE_PATH = Path(
    os.environ.get("SVL_KERNEL_STORE_PATH", Path(__file__).with_name(".svl_kernel_store.json"))
)
DEFAULT_STORE = {
    "config": {
        "sys_version": "9.0.0-Triada-BestOS-Mission",
        "mode": "Local-Deterministic-Production",
        "active_profiles": ["Alex", "Astyria", "si_1", "Aurelius"],
        "sovereign_mode": True,
    },
    "update_history": [],
    "chat_history": [],
}


def migrate_model_config(config: dict[str, Any]) -> bool:
    migrated = False
    for legacy_model_key in ("dialogue_engine", "ai_provider", "ai_model", "live_ai_engine"):
        if legacy_model_key in config:
            del config[legacy_model_key]
            migrated = True
    mode = config.get("mode")
    if isinstance(mode, str) and ("ollama" in mode.lower() or "gemini" in mode.lower()):
        config["mode"] = DEFAULT_STORE["config"]["mode"]
        migrated = True
    return migrated


def load_store() -> dict[str, Any]:
    if not STORE_PATH.exists():
        return json.loads(json.dumps(DEFAULT_STORE))
    with STORE_PATH.open("r", encoding="utf-8") as store_file:
        store = json.load(store_file)
    if not isinstance(store, dict):
        raise ValueError("DATA_STORE must be a JSON object")
    store.setdefault("config", {})
    store.setdefault("update_history", [])
    store.setdefault("chat_history", [])
    if not isinstance(store["config"], dict):
        raise ValueError("DATA_STORE.config must be an object")
    if not isinstance(store["update_history"], list) or not isinstance(store["chat_history"], list):
        raise ValueError("DATA_STORE histories must be arrays")
    if migrate_model_config(store["config"]):
        save_store(store)
    return store


def save_store(store: dict[str, Any]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = STORE_PATH.with_name(f"{STORE_PATH.name}.tmp")
    with temporary_path.open("w", encoding="utf-8") as store_file:
        json.dump(store, store_file, ensure_ascii=False, indent=2)
        store_file.write("\n")
    os.replace(temporary_path, STORE_PATH)


def add_update(store: dict[str, Any], version: str, description: str) -> None:
    store["update_history"].append(
        {
            "wersja": version,
            "data": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "opis": description,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action",
        choices=("state", "save_store", "config", "batch_msg", "clear_chat", "bump", "sync", "report"),
        default="state",
    )
    parser.add_argument("--store_json", default="{}")
    parser.add_argument("--config_json", default="{}")
    parser.add_argument("--msgs_json", default="[]")
    parser.add_argument("--version_tag", default="")
    parser.add_argument("--desc", default="")
    args = parser.parse_args()

    store = load_store()
    if args.action == "save_store":
        store = json.loads(args.store_json)
        if not isinstance(store, dict):
            raise ValueError("DATA_STORE must be a JSON object")
        if not isinstance(store.get("config"), dict):
            raise ValueError("DATA_STORE.config must be an object")
        if not isinstance(store.get("update_history"), list) or not isinstance(store.get("chat_history"), list):
            raise ValueError("DATA_STORE histories must be arrays")
        migrate_model_config(store["config"])
        save_store(store)
    elif args.action == "config":
        config = json.loads(args.config_json)
        if not isinstance(config, dict):
            raise ValueError("config_json must be a JSON object")
        store["config"].update(config)
        migrate_model_config(store["config"])
        if args.version_tag:
            store["config"]["sys_version"] = args.version_tag
            add_update(store, args.version_tag, args.desc)
        save_store(store)
    elif args.action == "batch_msg":
        messages = json.loads(args.msgs_json)
        if not isinstance(messages, list) or not all(isinstance(message, dict) for message in messages):
            raise ValueError("msgs_json must be an array of message objects")
        store["chat_history"].extend(messages)
        save_store(store)
    elif args.action == "clear_chat":
        store["chat_history"] = []
        save_store(store)
    elif args.action == "bump":
        if not args.version_tag:
            raise ValueError("version_tag is required for bump")
        store["config"]["sys_version"] = args.version_tag
        add_update(store, args.version_tag, args.desc)
        save_store(store)
    elif args.action == "sync":
        save_store(store)
    elif args.action == "report":
        print(
            f"SVL Kernel {store['config'].get('sys_version', 'unknown')}: "
            f"{len(store['chat_history'])} wiadomości, "
            f"{len(store['update_history'])} wydań."
        )
        return

    if args.action != "sync":
        print(
            json.dumps(
                {"store": store, "sys_version": store["config"].get("sys_version", "unknown")},
                ensure_ascii=False,
            )
        )
    else:
        print("DATA_STORE synchronized")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"SVL kernel error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
