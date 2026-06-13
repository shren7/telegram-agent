import os
import pathlib
import types
import urllib.parse

import dotenv


dotenv.load_dotenv()


with open(pathlib.Path.cwd() / "PROMPT.md") as file:
    _prompt = file.read()

if os.environ.get("PROXY", ""):
    _parsed_proxy = urllib.parse.urlparse(os.environ.get("PROXY", ""))
    _proxy = {
        "proxy_type": _parsed_proxy.scheme,
        "addr": _parsed_proxy.hostname,
        "port": _parsed_proxy.port,
        "username": _parsed_proxy.username,
        "password": _parsed_proxy.password,
    }
else:
    _proxy = {}

try:
    config = types.SimpleNamespace(
        SESSION_PATH=pathlib.Path.cwd() / "telegram.session",
        WAIT_SECONDS=int(os.environ.get("WAIT_SECONDS", 30)),
        PROXY=_proxy,
        PROMPT=_prompt,
        APP_ID=int(os.environ["APP_ID"]),
        APP_HASH=os.environ["APP_HASH"],
        # Must be OpenAI-compatible
        BASE_URL=os.environ["BASE_URL"],
        API_KEY=os.environ["API_KEY"],
        MODEL=os.environ["MODEL"],
    )
except KeyError:
    logger.critical(
        "You have forgot add some variables to .env! Here is error"
    )
    raise
