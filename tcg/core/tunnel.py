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
import subprocess
import threading
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


class SSMTunnel:
    """Manages the lifecycle of an AWS SSM port-forwarding subprocess."""

    def __init__(self, config: TunnelConfig) -> None:
        self._config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._drain_threads: list[threading.Thread] = []
        self._stderr_lines: list[str] = []
        self._stdout_lines: list[str] = []
        self._stopped = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the ``aws ssm start-session`` process."""
        self._stopped = False

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

        self._stderr_lines = []
        self._stdout_lines = []
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # Capture stderr in a daemon thread to prevent pipe-buffer
        # deadlocks.  Lines are kept so we can surface them in error
        # messages when the process dies during startup.
        self._start_stderr_capture_thread(self._process.stderr)

        # Safety net: kill the subprocess if Python exits without going
        # through the lifespan shutdown (crash, Ctrl+C during startup,
        # terminal closed, etc.).
        atexit.register(self._kill_process)
        self._install_signal_handlers()

    async def wait_until_ready(self, timeout: float = 30) -> None:
        """Block until the tunnel prints its ready marker or *timeout* elapses."""
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("SSM tunnel: process not started")

        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, self._read_stdout_until_ready),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            stdout_tail = (
                "\n".join(self._stdout_lines[-20:])
                if self._stdout_lines
                else "(no stdout output)"
            )
            stderr_tail = (
                "\n".join(self._stderr_lines[-20:])
                if self._stderr_lines
                else "(no stderr output)"
            )
            raise RuntimeError(
                f"SSM tunnel: did not become ready within {timeout}s.\n"
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

            # Process exited unexpectedly
            consecutive_failures += 1
            if consecutive_failures > _MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "SSM tunnel: %d consecutive failures, giving up",
                    consecutive_failures,
                )
                return

            logger.warning(
                "SSM tunnel: process exited, restarting (attempt %d)",
                consecutive_failures,
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
                logger.exception("SSM tunnel: restart failed")
                self._kill_process()

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_stdout_until_ready(self) -> None:
        """Blocking read of stdout until the ready marker appears.

        Called via ``run_in_executor`` so the async caller doesn't block.
        After detecting readiness, starts a drain thread for remaining
        stdout output.
        """
        assert self._process is not None and self._process.stdout is not None
        for raw_line in self._process.stdout:
            text = raw_line.decode(errors="replace").strip()
            if text:
                self._stdout_lines.append(text)
                logger.debug("SSM tunnel stdout: %s", text)
            if "Waiting for connections" in text:
                logger.info("SSM tunnel: ready")
                self._start_drain_thread(self._process.stdout)
                return
        # stdout closed without the ready marker — process exited.
        stderr_tail = (
            "\n".join(self._stderr_lines[-20:])
            if self._stderr_lines
            else "(no stderr output)"
        )
        raise RuntimeError(
            f"SSM tunnel: process exited before becoming ready.\nstderr:\n{stderr_tail}"
        )

    def _start_stderr_capture_thread(self, pipe: object) -> None:
        """Spawn a daemon thread that captures stderr lines for diagnostics."""

        def _capture() -> None:
            try:
                for raw_line in pipe:  # type: ignore[union-attr]
                    text = raw_line.decode(errors="replace").strip()  # type: ignore[union-attr]
                    if text:
                        self._stderr_lines.append(text)
                        logger.debug("SSM tunnel stderr: %s", text)
            except Exception:
                pass

        t = threading.Thread(target=_capture, daemon=True)
        t.start()
        self._drain_threads.append(t)

    def _start_drain_thread(self, pipe: object) -> None:
        """Spawn a daemon thread that reads and discards a pipe."""

        def _drain() -> None:
            try:
                while True:
                    chunk = pipe.read(4096)  # type: ignore[union-attr]
                    if not chunk:
                        break
            except Exception:
                pass

        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        self._drain_threads.append(t)

    def _install_signal_handlers(self) -> None:
        """Chain signal handlers so the tunnel subprocess is killed on SIGINT/SIGTERM.

        Preserves any existing handler (e.g. uvicorn's) and calls it after
        cleanup so normal shutdown still proceeds.
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
        """Terminate the subprocess if it's still alive."""
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
