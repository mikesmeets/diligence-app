"""
Gunicorn settings.

This file exists because the Procfile isn't always what starts the app — a
Custom Start Command configured in Railway overrides it, and that command
carries none of these flags. Gunicorn auto-loads gunicorn.conf.py from the
working directory whatever the start command is, so the timeout survives.

CLI flags still win over this file, so an explicit --timeout would take
precedence. Nothing here fights the start command; it only fills the gaps.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# A Claude write-up with thinking runs for minutes. Gunicorn's 30s default
# kills the worker mid-generation, which is what produced the 500s.
timeout = 300
graceful_timeout = 30

# One worker keeps memory down on a small container; threads stop a running
# generation from blocking every other request.
workers = 1
threads = 8
worker_class = 'gthread'

accesslog = '-'
errorlog = '-'
