"""DAQ file monitor. Start with: python monitor_file.py --config config.json"""
import argparse
import fnmatch
import json
import logging
import os
import re
import stat
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def human_size(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(value) < 1024 or unit == "PiB":
            return f"{value:.2f} {unit}"
        value /= 1024


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config.get("host"), str) or not config["host"]:
        raise ValueError("host must be a non-empty string")
    port = config.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    root = Path(config["file_path"])
    config["file_path"] = root if root.is_absolute() else path.parent / root
    extension = config["data_format"].lstrip(".")
    if not extension or any(c in extension for c in '/\\*?[]'):
        raise ValueError("Invalid data_format")
    config["data_format"] = extension
    for key in ("active_daq_states", "excluded_daq_states", "registered_daq_states"):
        states = config[key]
        if not isinstance(states, list) or any(not isinstance(s, str) or not s for s in states):
            raise ValueError(f"{key} must be a list of non-empty strings")
    if not set(config["active_daq_states"] + config["excluded_daq_states"]) <= set(config["registered_daq_states"]):
        raise ValueError("Active and excluded states must be registered")
    number_format = config.get("runnumber_format", "04d")
    if not isinstance(number_format, str) or not re.fullmatch(r"(?:0?[1-9][0-9]?)?d", number_format):
        raise ValueError("runnumber_format must be d or an integer format such as 04d (width 1-99)")
    config["runnumber_format"] = number_format
    pattern = config["filename_pattern"].format(runname="run", runnumber="001", data_format=extension)
    if any(c in pattern for c in '/\\'):
        raise ValueError("filename_pattern must not contain directories")
    return config


def create_app(config_path="config.json"):
    config = load_config(config_path)
    app = FastAPI(title="DAQ File Monitor", version="1.0.0")
    previous = {}
    lock = Lock()

    def response(status, message, **data):
        return {"status": status, "data": {"message": message, **data}}

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=response("fail", "Invalid query parameters"))

    @app.get("/monitor")
    def monitor(
        runname: str = Query(..., min_length=1, max_length=128, pattern=r"^[^/\\*?\[\]{}]+$"),
        runnumber: str = Query(..., min_length=1, max_length=32, pattern=r"^[0-9]+$"),
        daq_state: str = Query(..., min_length=1),
    ):
        # Keep the scan and snapshot replacement atomic across concurrent requests.
        with lock:
            try:
                if daq_state not in config["registered_daq_states"]:
                    return JSONResponse(status_code=400, content=response("fail", f"Unknown DAQ state (DAQ state: {daq_state})"))
                runnumber = format(int(runnumber), config["runnumber_format"])
                key = (runname, runnumber)
                active = daq_state in config["active_daq_states"]
                if not active:
                    previous.pop(key, None)
                # An explicitly active state must never silently skip monitoring.
                if not active and daq_state in config["excluded_daq_states"]:
                    previous.pop(key, None)
                    return response("ok", f"File monitoring is excluded (DAQ state: {daq_state})")
                pattern = config["filename_pattern"].format(runname=runname, runnumber=runnumber, data_format=config["data_format"])
                current = {}
                snapshot = {}
                # Scan only direct children; stat matching regular files without reading contents.
                with os.scandir(config["file_path"]) as entries:
                    for entry in entries:
                        if fnmatch.fnmatchcase(entry.name, pattern):
                            try:
                                info = entry.stat(follow_symlinks=False)
                            except FileNotFoundError:
                                continue  # A file may disappear during acquisition.
                            if stat.S_ISREG(info.st_mode):
                                current[entry.name] = info.st_size
                                snapshot[entry.name] = (info.st_size, info.st_mtime_ns)
                files = [{"name": name, "size": human_size(size)}
                         for name, size in sorted(current.items())]
                if not active:
                    return response("ok", f"File listing only; update monitoring is disabled (DAQ state: {daq_state})",
                                    file=files)
                old = previous.get(key)
                previous[key] = snapshot
                if not current:
                    return response("error", f"No matching files found (DAQ state: {daq_state})", file=files)
                if old is None:
                    return response("ok", f"Baseline recorded; updates will be checked on the next request (DAQ state: {daq_state})",
                                    file=files)
                changed = any(name not in old or metadata != old[name] for name, metadata in snapshot.items())
                if not changed:
                    return response("error", f"Files have not been updated (DAQ state: {daq_state})", file=files)
                return response("ok", f"Files updated (DAQ state: {daq_state})", file=files)
            except FileNotFoundError:
                previous.pop(key, None)
                if active:
                    return response("error", f"Data directory not found (DAQ state: {daq_state})", file=[])
                return response("ok", f"Data directory not found; update monitoring is disabled (DAQ state: {daq_state})", file=[])
            except Exception:
                logger.exception("File monitoring failed")
                return JSONResponse(status_code=500, content=response("fail", f"File monitoring failed (DAQ state: {daq_state})"))

    return app


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    args = parser.parse_args()
    settings = load_config(args.config)
    uvicorn.run(create_app(args.config), host=settings["host"], port=settings["port"])
