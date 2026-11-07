"""Local Engine API and existing Supervisor RPC; no creation or reconciliation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import quote

import aiohttp

from ..model import observation, readiness
from ..protocol import bridge_info, check_bridge

CLIENT = Path(__file__).with_name("controller_client.py").read_text()
LIMIT = 4 * 1024 * 1024


def demultiplex(data: bytes) -> bytes:
    output = bytearray()
    while data:
        if len(data) < 8 or data[0] not in (1, 2) or data[1:4] != b"\0\0\0":
            raise ValueError("Invalid Docker stream header")
        length = int.from_bytes(data[4:8], "big")
        if length > len(data) - 8:
            raise ValueError("Truncated Docker stream")
        output.extend(data[8 : 8 + length])
        data = data[8 + length :]
    return bytes(output)


def ready(info):
    state = info["State"]
    return bool(
        state.get("Running")
        and not state.get("Paused")
        and not state.get("Restarting")
        and state.get("Health", {}).get("Status", "healthy") == "healthy"
    )


def container_readiness(info):
    state = info["State"]
    health = state.get("Health", {}).get("Status")
    return readiness(
        "ready" if ready(info) else "not_ready",
        "docker health" if health is not None else "docker running",
        health if health is not None else "No Healthcheck; running state only",
    )


def epoch(info):
    return f"{info['Id']}@{info['State']['StartedAt']}"


def supervisor_children(prefix, reply, namespace="container"):
    result = []
    for process in reply["processes"]:
        key = f"{process['group']}:{process['name']}"
        if "*" in key:
            raise ValueError("Wildcard service names are unsupported")
        state = process["statename"]
        actions = (
            ["stop", "restart"]
            if state in ("RUNNING", "STARTING", "BACKOFF")
            else ["start", "restart"]
            if state in ("STOPPED", "EXITED", "FATAL")
            else []
        )
        result.append(
            dict(
                id=key,
                epoch=f"{prefix}/{reply['epoch']}",
                name=key,
                kind="service",
                parent=None,
                state=state,
                pid=process["pid"] or None,
                pid_namespace=namespace,
                cpu_percent=None,
                memory_bytes=None,
                runtime_id=None,
                exit_code=process["exitstatus"],
                actions=actions,
                reason="supervisord owns lifecycle; resources are shown separately, without PID namespace guessing",
            )
        )
    return result


def controller_children(prefix, controller, reply, namespace="container"):
    if controller["kind"] == "supervisord":
        return supervisor_children(prefix, reply, namespace)
    check_bridge(reply)
    result = reply["children"]
    for child in result:
        child["epoch"] = f"{prefix}/{child['epoch']}"
        child.setdefault("parent", None)
        child.setdefault("runtime_id", None)
    return result


class DockerAPI:
    def __init__(self, socket):
        self.client = aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=socket),
            timeout=aiohttp.ClientTimeout(total=60),
            trust_env=False,
        )
        self.version = None

    async def connect(self):
        try:
            _, info = await self.request("GET", ["version"], version=False)

            def parse(value):
                return tuple(int(v) for v in value.split("."))

            selected = max((1, 41), parse(info.get("MinAPIVersion", "1.24")))
            if selected > min((1, 52), parse(info["ApiVersion"])):
                raise RuntimeError("Docker API outside supported 1.41..1.52 range")
            self.version = f"v{selected[0]}.{selected[1]}"
        except BaseException:
            await self.client.close()
            raise
        return self

    async def request(self, method, path, *, data=None, params=None, raw=False, version=True):
        parts = ([self.version] if version else []) + list(path)
        url = "http://localhost/" + "/".join(quote(part, safe="") for part in parts)
        async with self.client.request(method, url, json=data, params=params) as response:
            result = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                if len(result) + len(chunk) > LIMIT:
                    raise RuntimeError("Docker reply exceeds 4 MiB")
                result.extend(chunk)
            if response.status >= 400 and response.status != 404:
                raise RuntimeError(f"Docker {response.status}: {result.decode(errors='replace')}")
            if raw:
                return response.status, bytes(result)
            return response.status, json.loads(result) if result else None

    async def resolve(self, config):
        if config.get("container"):
            target = config["container"]
        else:
            selector = config["compose"]
            filters = {
                "label": [
                    f"com.docker.compose.project={selector['project']}",
                    f"com.docker.compose.service={selector['service']}",
                ]
            }
            _, rows = await self.request(
                "GET",
                ["containers", "json"],
                params={"all": "true", "filters": json.dumps(filters)},
            )
            rows = [
                row
                for row in rows
                if row.get("Labels", {}).get("com.docker.compose.oneoff", "").lower() != "true"
            ]
            if len(rows) > 1:
                raise ValueError(
                    "Compose service resolves to multiple containers; choose an explicit container name"
                )
            if not rows:
                return None
            target = rows[0]["Id"]
        status, value = await self.request("GET", ["containers", target, "json"])
        return None if status == 404 else value

    async def operate(self, info, action):
        try:
            status, _ = await self.request("POST", ["containers", info["Id"], action])
            if status == 404:
                raise RuntimeError("Container disappeared")
        except Exception as error:
            raise RuntimeError(
                f"Docker {action} failed; outcome may be unknown; not retried: {error}"
            ) from error

    async def rpc(self, info, controller, request):
        status, created = await self.request(
            "POST",
            ["containers", info["Id"], "exec"],
            data={
                "AttachStdout": True,
                "AttachStderr": True,
                "Tty": False,
                "Cmd": [
                    controller.get("python", "python3"),
                    "-c",
                    CLIENT,
                    controller["kind"],
                    controller["socket"],
                    json.dumps(request),
                ],
            },
        )
        if status == 404:
            raise RuntimeError("Container disappeared")
        _, output = await self.request(
            "POST", ["exec", created["Id"], "start"], data={"Detach": False, "Tty": False}, raw=True
        )
        _, status = await self.request("GET", ["exec", created["Id"], "json"])
        reply = json.loads(demultiplex(output))
        if status.get("ExitCode") != 0 or not reply.get("ok"):
            raise RuntimeError(f"Controller: {reply.get('error', 'RPC failed; outcome unknown')}")
        return reply["data"]

    async def observe(self, work, info):
        state = info["State"]
        status = state.get("Status", "unknown")
        observation_state = {
            "running": "active",
            "restarting": "activating",
            "paused": "paused",
            "dead": "failed",
            "created": "inactive",
            "exited": "failed" if state.get("ExitCode") else "inactive",
            "removing": "deactivating",
        }.get(status, "unknown")
        result = observation(
            state=observation_state,
            sub_state=status,
            canonical_unit=info["Id"],
            invocation_id=state.get("StartedAt"),
            pid=state.get("Pid") or None,
            health=state.get("Health", {}).get("Status"),
            readiness=container_readiness(info),
        )
        if not state.get("Running"):
            return result
        try:
            _, stats = await self.request(
                "GET",
                ["containers", info["Id"], "stats"],
                params={"stream": "false", "one-shot": "true"},
            )
            result["memory_bytes"] = stats.get("memory_stats", {}).get("usage")
            result["cpu_nsec"] = stats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage")
        except (aiohttp.ClientError, OSError, TimeoutError, RuntimeError):
            pass
        try:
            _, top = await self.request(
                "GET",
                ["containers", info["Id"], "top"],
                params={"ps_args": "-eo pid,ppid,stat,pcpu,rss,comm"},
            )
            for raw in top["Processes"]:
                row = dict(zip(top["Titles"], raw))
                pid = int(row["PID"])
                result["children"].append(
                    dict(
                        id=f"process:{pid}",
                        epoch=epoch(info),
                        name=row.get("COMMAND", "process"),
                        kind="process",
                        parent=None,
                        state=row.get("STAT", "unknown"),
                        pid=pid,
                        pid_namespace="Docker daemon host",
                        cpu_percent=float(row["%CPU"]) if "%CPU" in row else None,
                        memory_bytes=int(row["RSS"]) * 1024 if "RSS" in row else None,
                        runtime_id=None,
                        exit_code=None,
                        actions=[],
                        reason="OS process observation only; CPU is ps lifetime average",
                    )
                )
        except Exception as error:
            result["children_error"] = f"Process inspection: {error}"
        if work.get("controller"):
            try:
                controller = work["controller"]
                reply = await self.rpc(info, controller, {"command": "snapshot"})
                if controller["kind"] == "roslaunch":
                    result["controller"] = bridge_info(reply)
                result["children"].extend(controller_children(epoch(info), controller, reply))
            except Exception as error:
                result["children_error"] = f"Controller unavailable; no repair attempted: {error}"
                result["observer_error"] = result["children_error"]
        result["children"].sort(key=lambda child: (child["kind"] == "process", child["name"]))
        return result

    async def logs(self, info):
        status, output = await self.request(
            "GET",
            ["containers", info["Id"], "logs"],
            params={"stdout": "true", "stderr": "true", "tail": "150"},
            raw=True,
        )
        if status == 404:
            raise RuntimeError("Container disappeared")
        return (output if info.get("Config", {}).get("Tty") else demultiplex(output)).decode(
            errors="replace"
        )

    async def close(self):
        await self.client.close()


async def host_rpc(controller, request):
    process = await asyncio.create_subprocess_exec(
        controller.get("python", "python3"),
        "-c",
        CLIENT,
        controller["kind"],
        controller["socket"],
        json.dumps(request),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    if len(stdout) > LIMIT:
        raise RuntimeError("Controller reply too large")
    reply = json.loads(stdout)
    if process.returncode or not reply.get("ok"):
        raise RuntimeError(
            f"Controller RPC failed; outcome may be unknown: {reply.get('error', stderr.decode(errors='replace'))}"
        )
    return reply["data"]
