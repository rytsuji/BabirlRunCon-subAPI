#!/usr/bin/env python3
"""HTTP API for the MPV Restart babildes button."""
import argparse
import ipaddress
import json
import logging
from pathlib import Path
import threading
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("host", "mpv_ip"):
        ipaddress.ip_address(config[key])
    if type(config.get("port")) is not int or not 1 <= config["port"] <= 65535:
        raise ValueError("port must be an integer in 1..65535")
    return config


def create_app(config):
    app = FastAPI(title="MPV Restart API")
    lock = threading.Lock()
    address = ipaddress.ip_address(config["mpv_ip"])
    host = f"[{address}]" if address.version == 6 else str(address)
    url = f"http://{host}/cgi-bin/index.cgi"
    # Connect directly to MPV and never follow a redirect for this operation.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def response(status, message, code=200):
        return JSONResponse({"status": status, "data": {"message": message},
                             "success": status == "ok"}, status_code=code)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc):
        return response("fail", str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def api_error(request: Request, exc):
        logging.exception("MPV API failed")
        return response("fail", "API failed", 500)

    @app.post("/restart")
    def restart():
        if not lock.acquire(blocking=False):
            return response("fail", "another restart request is in progress", 409)
        try:
            data = urllib.parse.urlencode({"restartbabildes": "Restart babildes"}).encode("ascii")
            request = urllib.request.Request(url, data=data, method="POST",
                                             headers={"Content-Type": "application/x-www-form-urlencoded"})
            with opener.open(request, timeout=15) as result:
                if result.status != 200:
                    return response("fail", f"MPV returned HTTP {result.status}", 502)
            return response("ok", "MPV accepted the Restart babildes request; process restart is not verified")
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            return response("fail", f"MPV returned HTTP {code}", 502)
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            timeout = isinstance(reason, TimeoutError)
            return response("fail", "MPV request timed out; restart may have been executed" if timeout
                            else f"MPV request failed; restart may have been executed: {reason}",
                            504 if timeout else 503)
        finally:
            lock.release()

    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    uvicorn.run(create_app(config), host=config["host"], port=config["port"], workers=1)


if __name__ == "__main__":
    main()
