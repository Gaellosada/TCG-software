"""SSM port-forwarding tunnel for private MongoDB access.

Spawns an ``aws ssm start-session`` subprocess that forwards a local TCP
port to a remote MongoDB host via an EC2 bastion.  The tunnel is started
during FastAPI lifespan startup and stopped on shutdown.  A background
``monitor()`` task restarts the tunnel automatically on unexpected exit.

Uses ``subprocess.Popen`` (not ``asyncio.create_subprocess_exec``) because
Windows' asyncio ``ProactorEventLoop`` does not support subprocess pipes
under uvicorn's reload-mode event loop.  Pipe reading is offloaded to
daemon threads so the async caller never blocks.
"""

from __future__ import annotations

import atexit
import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, repr=False)
class TunnelConfig:
    """All values needed to open an SSM port-forwarding tunnel."""

    enabled: bool
    bastion_id: str
    db_host: str
    db_port: str
    local_port: str
    region: str
    aws_access_key_id: str
    aws_secret_access_key: str

    def __repr__(self) -> str:
        return (
            f"TunnelConfig(enabled={self.enabled}, bastion_id={self.bastion_id!r}, "
            f"region={self.region!r}, local_port={self.local_port!r}, "
            f"aws_access_key_id='***', aws_secret_access_key='***')"
        )


_MAX_CONSECUTIVE_FAILURES = 5
_BACKOFF_CAP_SECONDS = 30.0
_CAPTURED_LINES_MAX = 100


class SSMTunnel:
    """Manages the lifecycle of an AWS SSM port-forwarding subprocess."""

    def __init__(self, config: TunnelConfig) -> None:
        self._config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._drain_threads: list[threading.Thread] = []
        self._stderr_lines: deque[str] = deque(maxlen=_CAPTURED_LINES_MAX)
        self._stdout_lines: deque[str] = deque(maxlen=_CAPTURED_LINES_MAX)
        self._handlers_installed = False
        self._stopped = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the ``aws ssm start-session`` process."""
        self._stopped = False

        # Kill any prior process before spawning a new one.
        self._kill_process()
        # Wait for old capture threads to finish (pipes close when process
        # dies) and clear the list so it doesn't grow across restarts.
        for t in self._drain_threads:
            t.join(timeout=2)
        self._drain_threads.clear()

        aws_bin = shutil.which("aws")
        if aws_bin is None:
            raise RuntimeError(
                "SSM tunnel: 'aws' CLI not found in PATH. "
                "Install AWS CLI v2: https://aws.amazon.com/cli/"
            )

        cfg = self._config
        port = int(cfg.local_port)  # validated upstream, but ensure int

        parameters = json.dumps(
            {
                "host": [cfg.db_host],
                "portNumber": [cfg.db_port],
                "localPortNumber": [str(port)],
            }
        )

        cmd = [
            aws_bin,
            "ssm",
            "start-session",
            "--target",
            cfg.bastion_id,
            "--document-name",
            "AWS-StartPortForwardingSessionToRemoteHost",
            "--parameters",
            parameters,
            "--region",
            cfg.region,
        ]

        env = {
            **os.environ,
            "AWS_ACCESS_KEY_ID": cfg.aws_access_key_id,
            "AWS_SECRET_ACCESS_KEY": cfg.aws_secret_access_key,
            "AWS_REGION": cfg.region,
        }

        logger.info("SSM tunnel: starting on localhost:%s", port)

        self._stderr_lines.clear()
        self._stdout_lines.clear()
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # Capture both streams in daemon threads to prevent pipe-buffer
        # deadlocks.  Lines are kept for diagnostic error messages.
        self._start_capture_thread(self._process.stdout, self._stdout_lines, "stdout")
        self._start_capture_thread(self._process.stderr, self._stderr_lines, "stderr")

        # Safety net: kill the subprocess if Python exits without going
        # through the lifespan shutdown.  Register only once to avoid
        # accumulating duplicate handlers across restarts.
        if not self._handlers_installed:
            atexit.register(self._kill_process)
            self._install_signal_handlers()
            self._handlers_installed = True

    async def wait_until_ready(self, timeout: float = 30) -> None:
        """Poll the local forwarded port until a TCP connection succeeds.

        This is more robust than parsing stdout/stderr for a ready marker
        because the Session Manager plugin's output format, buffering, and
        stream routing vary across versions and platforms.
        """
        if self._process is None:
            raise RuntimeError("SSM tunnel: process not started")

        port = int(self._config.local_port)
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, self._poll_port, port),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            stdout_tail = (
                "\n".join(self._stdout_lines)
                if self._stdout_lines
                else "(no stdout output)"
            )
            stderr_tail = (
                "\n".join(self._stderr_lines)
                if self._stderr_lines
                else "(no stderr output)"
            )
            raise RuntimeError(
                f"SSM tunnel: port {port} not reachable within {timeout}s.\n"
                f"stdout:\n{stdout_tail}\n"
                f"stderr:\n{stderr_tail}"
            ) from None

    async def stop(self) -> None:
        """Terminate the tunnel subprocess."""
        self._stopped = True
        self._kill_process()
        atexit.unregister(self._kill_process)

    async def monitor(self) -> None:
        """Long-lived task: restart the tunnel on unexpected exit with backoff."""
        consecutive_failures = 0
        backoff = 1.0

        while not self._stopped:
            await asyncio.sleep(1)

            if self._process is not None and self._process.poll() is None:
                continue  # Still running

            # Process exited unexpectedly (or was never started after a
            # failed restart — _kill_process sets _process to None).
            consecutive_failures += 1
            if consecutive_failures > _MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "SSM tunnel: %d consecutive failures, giving up",
                    consecutive_failures,
                )
                return

            logger.warning(
                "SSM tunnel: restarting (attempt %d/%d)",
                consecutive_failures,
                _MAX_CONSECUTIVE_FAILURES,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP_SECONDS)

            try:
                await self.start()
                await self.wait_until_ready()
                # Restart succeeded — reset backoff
                consecutive_failures = 0
                backoff = 1.0
            except Exception:
                logger.warning(
                    "SSM tunnel: restart attempt %d failed: %s",
                    consecutive_failures,
                    type(Exception).__name__,
                )
                self._kill_process()

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _poll_port(self, port: int) -> None:
        """Blocking TCP poll until ``localhost:port`` accepts a connection.

        Called via ``run_in_executor``.  Also watches for early process
        death so we don't wait the full timeout if the tunnel crashes.
        """
        while True:
            # If the process died, raise immediately with diagnostics.
            if self._process is not None and self._process.poll() is not None:
                stderr_tail = (
                    "\n".join(self._stderr_lines)
                    if self._stderr_lines
                    else "(no stderr output)"
                )
                raise RuntimeError(
                    "SSM tunnel: process exited before the port became reachable.\n"
                    f"stderr:\n{stderr_tail}"
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    logger.info("SSM tunnel: ready (port %d reachable)", port)
                    return
            except OSError:
                time.sleep(0.5)

    def _start_capture_thread(self, pipe: object, dest: deque[str], label: str) -> None:
        """Spawn a daemon thread that captures pipe lines into *dest*."""

        def _capture() -> None:
            try:
                for raw_line in pipe:  # type: ignore[union-attr]
                    text = raw_line.decode(errors="replace").strip()  # type: ignore[union-attr]
                    if text:
                        dest.append(text)
                        logger.debug("SSM tunnel %s: %s", label, text)
            except Exception:
                logger.debug("SSM tunnel %s capture ended", label)

        t = threading.Thread(target=_capture, daemon=True)
        t.start()
        self._drain_threads.append(t)

    def _install_signal_handlers(self) -> None:
        """Chain signal handlers so the tunnel subprocess is killed on SIGINT/SIGTERM.

        Preserves any existing handler (e.g. uvicorn's) and calls it after
        cleanup so normal shutdown still proceeds.  Called only once — the
        ``_handlers_installed`` guard in ``start()`` prevents stacking.
        """
        for sig in (signal.SIGINT, signal.SIGTERM):
            prev = signal.getsignal(sig)

            def _handler(signum: int, frame: object, _prev: object = prev) -> None:
                self._kill_process()
                # Re-invoke the previous handler so uvicorn (or Python's
                # default KeyboardInterrupt) still fires normally.
                if callable(_prev):
                    _prev(signum, frame)
                elif _prev == signal.SIG_DFL:
                    signal.signal(signum, signal.SIG_DFL)
                    os.kill(os.getpid(), signum)

            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                # signal.signal() can only be called from the main thread.
                # If we're not on the main thread, atexit is still in place.
                pass

    def _kill_process(self) -> None:
        """Terminate the subprocess **and all its children**.

        Thread-safe: may be called concurrently from signal handlers,
        ``atexit``, and the ``stop()`` / ``monitor()`` methods.

        ``proc.terminate()`` on Windows only kills the parent ``aws``
        process — the ``session-manager-plugin.exe`` child is orphaned
        and keeps the forwarded port open.  We use ``taskkill /T /F``
        to kill the entire process tree, matching the launcher's cleanup
        strategy.
        """
        with self._lock:
            proc = self._process
            if proc is None:
                return
            self._process = None

        # Outside the lock — terminate/wait can take time.
        if proc.poll() is None:
            # Kill the entire process tree (aws + session-manager-plugin).
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                # Fallback to basic kill if taskkill is unavailable.
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
