import threading

from app import app, init_db, scheduler


# Un solo worker Gunicorn debe importar este módulo para mantener un scheduler único.
init_db()
threading.Thread(target=scheduler, daemon=True, name="autoweb-scheduler").start()
