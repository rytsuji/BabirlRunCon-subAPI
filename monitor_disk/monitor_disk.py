"""Disk usage monitor. Start with: python monitor_disk.py --config config.json"""
import argparse
import json
import logging
import math
import re
import shutil
from pathlib import Path

from fastapi import FastAPI

logger = logging.getLogger(__name__)


def human_size(size):
    value = size / 1000 ** 3
    for unit in ("GB", "TB", "PB", "EB"):
        if value < 1000 or unit == "EB":
            return f"{value:.2f} {unit}"
        value /= 1000


def parse_limit(value):
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(%|[KMGTPE]?I?B)\s*", str(value), re.IGNORECASE)
    if not match:
        raise ValueError("limit must be null or a capacity such as '500 GB', '2 TB', or '90%'")
    number = float(match[1])
    unit = match[2].upper()
    if not math.isfinite(number) or (unit == "%" and number > 100):
        raise ValueError("limit must be finite; percentage must be between 0 and 100")
    if unit == "%":
        return number, "%"
    power = "BKMGTPE".index(unit[0])
    return number * (1024 if "I" in unit else 1000) ** power, "bytes"


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config.get("host"), str) or not config["host"]:
        raise ValueError("host must be a non-empty string")
    if type(config.get("port")) is not int or not 1 <= config["port"] <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    if not isinstance(config.get("disks"), list) or not config["disks"]:
        raise ValueError("disks must be a non-empty list")
    for disk in config["disks"]:
        if not isinstance(disk.get("path"), str) or not disk["path"]:
            raise ValueError("Each disk must have a non-empty path")
        root = Path(disk["path"])
        disk["path"] = root if root.is_absolute() else path.parent / root
        disk["threshold"] = parse_limit(disk.get("limit"))
    return config


def create_app(config_path="config.json"):
    config = load_config(config_path)
    app = FastAPI(title="DAQ Disk Monitor", version="1.0.0")

    @app.get("/monitor")
    def monitor():
        disks = []
        exceeded_paths = []
        failed_paths = []
        for disk in config["disks"]:
            entry = {"path": str(disk["path"])}
            try:
                if not disk["path"].is_dir():
                    raise OSError("Directory does not exist or is inaccessible")
                usage = shutil.disk_usage(disk["path"])
                percent = usage.used / usage.total * 100
                threshold = disk["threshold"]
                exceeded = threshold is not None and (
                    percent >= threshold[0] if threshold[1] == "%" else usage.used >= threshold[0]
                )
                entry["usage"] = f"{human_size(usage.used)} / {human_size(usage.total)} ({percent:.2f}%)"
                if exceeded:
                    exceeded_paths.append(entry["path"])
            except OSError as exc:
                logger.warning("Disk monitoring failed for %s: %s", disk["path"], exc)
                entry["usage"] = "Unavailable"
                failed_paths.append(entry["path"])
            disks.append(entry)
        status = "fail" if failed_paths else "error" if exceeded_paths else "ok"
        messages = []
        if failed_paths:
            messages.append("Cannot read disk usage: " + ", ".join(failed_paths))
        if exceeded_paths:
            messages.append("Disk usage limit reached: " + ", ".join(exceeded_paths))
        return {"status": status, "data": {
            "message": "; ".join(messages) or "Disk usage retrieved", "disk": disks,
        }}

    return app


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    args = parser.parse_args()
    settings = load_config(args.config)
    uvicorn.run(create_app(args.config), host=settings["host"], port=settings["port"])
