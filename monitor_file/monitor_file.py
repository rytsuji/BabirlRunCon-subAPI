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


def natural_sort_key(path):
    """Compare digit sequences numerically, with other text compared literally."""
    return tuple((1, int(part)) if part.isdigit() else (0, part)
                 for part in re.split(r"([0-9]+)", path))


def human_size(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(value) < 1024 or unit == "PiB":
            return f"{value:.2f} {unit}"
        value /= 1024


def run_file_sizes(root, pattern, recursive):
    """Sizes of regular files matching pattern, keyed by path relative to root."""
    sizes = {}

    def scan(directory):
        with os.scandir(directory) as entries:
            for entry in entries:
                if recursive and entry.is_dir(follow_symlinks=False):
                    try:
                        scan(Path(entry.path))
                    except FileNotFoundError:
                        continue
                elif fnmatch.fnmatchcase(entry.name, pattern):
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(info.st_mode):
                        sizes[Path(entry.path).relative_to(root).as_posix()] = info.st_size

    scan(root)
    return sizes


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
    recursive = config.get("recursive", False)
    if type(recursive) is not bool:
        raise ValueError("recursive must be a boolean")
    config["recursive"] = recursive
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
                # Relative paths distinguish identical filenames in different directories.
                root = config["file_path"]

                def scan(directory):
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if config["recursive"] and entry.is_dir(follow_symlinks=False):
                                try:
                                    scan(Path(entry.path))
                                except FileNotFoundError:
                                    continue  # A directory may disappear during acquisition.
                            elif fnmatch.fnmatchcase(entry.name, pattern):
                                try:
                                    info = entry.stat(follow_symlinks=False)
                                except FileNotFoundError:
                                    continue  # A file may disappear during acquisition.
                                if stat.S_ISREG(info.st_mode):
                                    name = Path(entry.path).relative_to(root).as_posix()
                                    current[name] = info.st_size
                                    snapshot[name] = (info.st_size, info.st_mtime_ns)

                scan(root)
                files = [{"name": name, "size": human_size(size)}
                         for name, size in sorted(current.items(),
                                                  key=lambda item: (natural_sort_key(item[0]), item[0]))]
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

    @app.get("/size")
    def size(
        runname: str = Query(..., min_length=1, max_length=128, pattern=r"^[^/\\*?\[\]{}]+$"),
        runnumber: str = Query(..., min_length=1, max_length=32, pattern=r"^[0-9]+$"),
    ):
        """Total size of one Run's files, for the Web GUI File size API."""
        pattern = config["filename_pattern"].format(runname=runname, runnumber=format(int(runnumber), config["runnumber_format"]),
                                                    data_format=config["data_format"])
        try:
            sizes = run_file_sizes(config["file_path"], pattern, config["recursive"])
        except FileNotFoundError:
            return response("error", "Data directory not found", bytes=None, file=[])
        except Exception:
            logger.exception("File size scan failed")
            return JSONResponse(status_code=500, content=response("fail", "File size scan failed"))
        if not sizes:
            return response("error", "No matching files found", bytes=None, file=[])
        files = [{"name": name, "size": human_size(size)}
                 for name, size in sorted(sizes.items(), key=lambda item: (natural_sort_key(item[0]), item[0]))]
        total = sum(sizes.values())
        return response("ok", f"Total {human_size(total)}", bytes=total, file=files)

    return app


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    args = parser.parse_args()
    settings = load_config(args.config)
    uvicorn.run(create_app(args.config), host=settings["host"], port=settings["port"])
