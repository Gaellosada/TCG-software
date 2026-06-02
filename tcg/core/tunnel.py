"""SSM port-forwarding tunnel for private MongoDB access.

Spawns an ``aws ssm start-session`` subprocess that forwards a local TCP
port to a remote MongoDB host via an EC2 bastion.  The tunnel is started
during FastAPI lifespan startup and stopped on shutdown.  A background
``monitor()`` task restarts the tunnel automatically on unexpected exit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
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
        self._process: asyncio.subprocess.Process | None = None
        self._drain_stdout_task: asyncio.Task[None] | None = None
        self._drain_stderr_task: asyncio.Task[None] | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the ``aws ssm start-session`` process."""
        self._stopped = False
        self._cancel_drain_tasks()

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

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Drain stderr immediately to prevent pipe-buffer deadlocks.
        # The SSM process may write auth errors or SDK debug messages to
        # stderr before printing the ready marker on stdout.
        self._drain_stderr_task = asyncio.create_task(
            self._drain_pipe(self._process.stderr)
        )

    async def wait_until_ready(self, timeout: float = 30) -> None:
        """Block until the tunnel prints its ready marker or *timeout* elapses."""
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("SSM tunnel: process not started")

        async def _read_until_ready() -> None:
            assert self._process is not None and self._process.stdout is not None
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    # Process closed stdout without printing the ready marker.
                    raise RuntimeError(
                        "SSM tunnel: process exited before becoming ready. "
                        "Check AWS credentials, bastion ID, and Session Manager plugin."
                    )
                text = line.decode(errors="replace").strip()
                if "Waiting for connections" in text:
                    logger.info("SSM tunnel: ready")
                    # Drain remaining stdout in the background.
                    self._drain_stdout_task = asyncio.create_task(
                        self._drain_pipe(self._process.stdout)
                    )
                    return

        try:
            await asyncio.wait_for(_read_until_ready(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"SSM tunnel: did not become ready within {timeout}s. "
                "Check AWS credentials, bastion ID, and network connectivity."
            ) from None

    async def stop(self) -> None:
        """Terminate the tunnel subprocess and cancel drain tasks."""
        self._stopped = True
        self._cancel_drain_tasks()
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
            self._process = None

    async def monitor(self) -> None:
        """Long-lived task: restart the tunnel on unexpected exit with backoff."""
        consecutive_failures = 0
        backoff = 1.0

        while not self._stopped:
            await asyncio.sleep(1)

            if self._process is not None and self._process.returncode is None:
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
                # Kill the process so the next iteration detects it as dead
                # and retries, rather than seeing it as "running."
                if self._process is not None and self._process.returncode is None:
                    self._process.terminate()
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        self._process.kill()

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _cancel_drain_tasks(self) -> None:
        """Cancel and clear any active drain tasks."""
        for task in (self._drain_stdout_task, self._drain_stderr_task):
            if task is not None:
                task.cancel()
        self._drain_stdout_task = None
        self._drain_stderr_task = None

    @staticmethod
    async def _drain_pipe(stream: asyncio.StreamReader | None) -> None:
        """Read and discard a pipe so the OS buffer doesn't fill up."""
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
        except asyncio.CancelledError:
            return
