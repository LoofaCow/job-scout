"""
Dev entry point — starts the FastAPI server with auto-reload.

For production deployment we'd use a different launcher (gunicorn, systemd
service, etc.), but for desktop-app dev this is what you run.
"""

import logging

import uvicorn


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    uvicorn.run(
        "app.api:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_level="info",
    )