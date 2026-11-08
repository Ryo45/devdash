#!/usr/bin/env python3
"""Bounded, read-only ExecStartPost probe. Never creates, starts or stops a container."""

import argparse
import json
import subprocess
import sys
import time


class HealthError(RuntimeError):
    pass


def inspect_container(docker, container, timeout):
    try:
        reply = subprocess.run(
            [docker, "inspect", "--type", "container", container],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        return json.loads(reply.stdout)[0]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as error:
        raise HealthError(f"Docker inspection unavailable: {error}") from error


def wait_health(container, *, docker="/usr/bin/docker", timeout=45, interval=0.5):
    deadline = time.monotonic() + timeout
    identity = invocation = None
    print(f"Waiting for Docker Health: {container}", flush=True)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HealthError(f"Docker Health deadline exceeded ({timeout}s): {container}")
        info = inspect_container(docker, container, min(remaining, 5))
        state = info["State"]
        if identity is not None and identity != info["Id"]:
            raise HealthError("Container replaced during startup; refusing replacement")
        identity = info["Id"]
        if invocation is not None and invocation != state.get("StartedAt"):
            raise HealthError("Container restarted during startup")
        if state.get("Running") and not state.get("Paused") and not state.get("Restarting"):
            invocation = state.get("StartedAt")
            health = state.get("Health", {}).get("Status")
            if health == "healthy":
                # A slow inspect must not turn an expired deadline into success.
                if time.monotonic() >= deadline:
                    raise HealthError("Docker Health deadline exceeded during inspection")
                print(f"Docker Health healthy: {identity}", flush=True)
                return
            if health != "starting":
                raise HealthError(f"Docker Health is {health or 'not configured'}: {container}")
        elif invocation is not None:
            raise HealthError(f"Container left Running before readiness: {state.get('Status')}")
        # Type=exec may enter ExecStartPost while docker start is still contacting Engine.
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("container", help="existing, exclusively unit-owned container name or ID")
    parser.add_argument("--docker", default="/usr/bin/docker")
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()
    if not 0 < args.timeout < float("inf") or not 0 < args.interval < float("inf"):
        parser.error("timeout and interval must be finite and positive")
    try:
        wait_health(
            args.container, docker=args.docker, timeout=args.timeout, interval=args.interval
        )
    except (HealthError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
