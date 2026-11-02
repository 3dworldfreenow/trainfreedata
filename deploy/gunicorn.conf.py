# Gunicorn settings for the RailMon web process (systemd: railmon-web.service)
bind = "127.0.0.1:8000"
workers = 3
threads = 4
worker_class = "gthread"
timeout = 60
graceful_timeout = 30
max_requests = 2000
max_requests_jitter = 200
accesslog = "-"
errorlog = "-"
forwarded_allow_ips = "127.0.0.1"
