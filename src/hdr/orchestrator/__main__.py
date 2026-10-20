"""python -m hdr.orchestrator  ->  simulator API on 127.0.0.1:8766"""

import os

import uvicorn

from hdr.orchestrator.api import api_from_env


def main() -> None:
    uvicorn.run(
        api_from_env(),
        host=os.environ.get("HDR_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("HDR_API_PORT", "8766")),
        log_level=os.environ.get("HDR_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
