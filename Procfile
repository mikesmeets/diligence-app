# --timeout 300: a Claude write-up with thinking runs well past gunicorn's
#   30s default, which would otherwise kill the worker mid-generation.
# --threads 4: so a running generation doesn't block every other request.
web: gunicorn app:app --bind 0.0.0.0:$PORT --timeout 300 --workers 2 --threads 4
