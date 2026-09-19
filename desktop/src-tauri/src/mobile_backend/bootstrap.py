"""Start la musica's API inside the app's own interpreter.

Run by `mobile_backend.rs` through `PyRun_SimpleString` once CPython is
initialised and PYTHONHOME/PYTHONPATH point at the bundled runtime. Everything
here is deliberately small: the real work is `server.main:app`, which on desktop
is started by `uvicorn` from the command line and here by the same `uvicorn`
called as a library.

Two things this does differently from the desktop launch, both forced by the
platform:

* It runs the server on a **thread**, never on the interpreter's main thread.
  The Rust side releases the GIL immediately after this returns, so the server
  thread is what steps the event loop; blocking the main thread would freeze the
  UI instead of serving anything.
* It never returns — the app either answers requests until the process ends or
  the thread dies with the traceback in the device log (logcat on Android, the
  Console on iOS), which is the only diagnostics channel an app has.

`MLO_ALLOW_SHUTDOWN=1` is set by the shell before this runs, so the backend's
own `/api/shutdown` route is live and the shell can ask for a clean exit (that
route is env-gated in server/main.py, exactly as on desktop).
"""
import threading
import traceback


def _serve():
    try:
        import uvicorn
        from server.main import app
    except Exception:
        # A missing dependency is a bundle bug, and the message has to survive in
        # the log: nothing else is listening for it.
        print("[mlo-mobile] cannot import the backend:", flush=True)
        traceback.print_exc()
        return
    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=8000,
            log_level="info",
            access_log=False,
        )
    except Exception:
        print("[mlo-mobile] the backend stopped with an exception:", flush=True)
        traceback.print_exc()


threading.Thread(target=_serve, name="mlo-backend", daemon=True).start()
print("[mlo-mobile] backend thread started", flush=True)
