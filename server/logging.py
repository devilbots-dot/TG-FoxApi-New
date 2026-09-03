
import logging

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s - %(levelname)s] - %(name)s - %(message)s",
    datefmt="%d-%b-%y %H:%M:%S",
    handlers=[
        logging.FileHandler("log.txt"),
        logging.StreamHandler(),
    ],
)

logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("pytgcalls").setLevel(logging.ERROR)

# Attach in-memory ring buffer for live log streaming in admin panel
try:
    from server.utils.logbuffer import attach_log_buffer
    attach_log_buffer()
except Exception:
    pass


def LOGGER(name: str) -> logging.Logger:
    return logging.getLogger(name)