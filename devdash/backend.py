"""Explicit adapter dispatch; intentionally no plugin framework."""

from __future__ import annotations

import asyncio
import json
import time

from .adapters.docker import DockerAPI, controller_children, epoch, host_rpc, ready
from .adapters.systemd import Systemd
from .model import observation


class Backends:
    def __init__(self, config):
        self.systemd = Systemd(config)
        self.apis = {}
        self.lock = asyncio.Lock()
        self.requests = {}
        self._emit = lambda event: None

    @property
    def emit(self):
        return self._emit

    @emit.setter
    def emit(self, emit):
        self._emit = emit
        self.systemd.emit = emit

    @property
    def revision(self):
        return self.systemd.revision

    def health(self):
        return self.systemd.health()

    def lease(self, scope):
        return self.systemd.lease(scope)

    async def start(self):
        await self.systemd.start()

    def requested(self, work):
        now = time.monotonic()
        self.requests = {key: value for key, value in self.requests.items() if now - value < 5}
        self.requests[json.dumps(work, sort_keys=True)] = now

    def recent(self, work):
        return (
            work["backend"] == "systemd"
            and self.systemd.recent(work)
            or time.monotonic() - self.requests.get(json.dumps(work, sort_keys=True), -100) < 5
        )

    async def api(self, work):
        path = work["docker"]["socket"]
        async with self.lock:
            if path not in self.apis:
                try:
                    self.apis[path] = await DockerAPI(path).connect()
                except Exception as error:
                    raise RuntimeError(
                        f"Docker socket {path} unavailable; check permissions: {error}"
                    ) from error
            return self.apis[path]

    async def resolved(self, work):
        api = await self.api(work)
        info = await api.resolve(work["docker"])
        if info is None:
            raise RuntimeError("Container not found; no container was created")
        return api, info

    async def operate(self, work, action, lease=None):
        self.requested(work)
        if work["backend"] == "systemd":
            return await self.systemd.operate(work, action, lease)
        api, info = await self.resolved(work)
        return await api.operate(info, action)

    async def observe(self, work):
        if work["backend"] == "docker":
            api = await self.api(work)
            info = await api.resolve(work["docker"])
            return await api.observe(work, info) if info else observation(state="not_found")
        result = await self.systemd.observe(work)
        if controller := work.get("controller"):
            try:
                reply = await host_rpc(controller, {"command": "snapshot"})
                result["children"] = controller_children(
                    "host:" + work["scope"] + ":" + work["unit"],
                    controller,
                    reply,
                    namespace="host supervisor",
                )
            except Exception as error:
                result["children_error"] = str(error)
        return result

    async def readiness(self, work):
        if work["backend"] == "docker":
            api = await self.api(work)
            info = await api.resolve(work["docker"])
            return info is not None and ready(info)
        return (await self.systemd.observe(work))["state"] == "active"

    async def operate_child(self, work, child_id, expected_epoch, action):
        controller = work.get("controller")
        if not controller:
            raise ValueError("No controller configured; OS processes are observation-only")
        if work["backend"] == "docker":
            api, info = await self.resolved(work)
            prefix = epoch(info)

            def rpc(request):
                return api.rpc(info, controller, request)
        else:
            prefix = "host:" + work["scope"] + ":" + work["unit"]

            def rpc(request):
                return host_rpc(controller, request)

        children = controller_children(prefix, controller, await rpc({"command": "snapshot"}))
        child = next((child for child in children if child["id"] == child_id), None)
        if child is None:
            raise ValueError("Unknown logical service; raw PIDs cannot be operated")
        if child["epoch"] != expected_epoch:
            raise ValueError("Container/controller changed; refresh before operating")
        if action not in child["actions"]:
            raise ValueError("Operation unsupported: " + child.get("reason", "controller state"))
        self.requested(work)
        await rpc(
            {
                "command": "operate",
                "id": child_id,
                "epoch": expected_epoch[len(prefix) + 1 :],
                "action": action,
            }
        )

    async def scan(self, scope):
        return await self.systemd.scan(scope)

    async def dependencies(self, seeds):
        return await self.systemd.dependencies(seeds)

    async def logs(self, work):
        if work["backend"] == "systemd":
            return await self.systemd.logs(work)
        api, info = await self.resolved(work)
        return await api.logs(info)

    async def close(self):
        await self.systemd.close()
        await asyncio.gather(*(api.close() for api in self.apis.values()))
