# gunicorn_conf.py
from multiprocessing import cpu_count

# Bind to all interfaces so the container is reachable from the network.
# With network_mode: host this means the NAS host's port 8000 is directly exposed.
bind = "0.0.0.0:8000"

# Worker Options
workers = cpu_count() + 1
worker_class = "uvicorn.workers.UvicornWorker"

# Logging Options
# FIXME: make these all configurable in conf
loglevel = "debug"
accesslog = "/tmp/soundcork_access.log"
errorlog = "/tmp/soundcork_error.log"
