"""Explicit adapter dispatch; intentionally no plugin framework."""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp

from .adapters.docker import DockerAPI, controller_children, epoch, host_rpc
from .adapters.systemd import Systemd
from .model import docker_reference, observation
from .protocol import bridge_info
from .self_protection import SelfManagementError


class Backends:
    def __init__(self, config):
        self.systemd = Systemd(config)
        self.apis = {}
        self.lock = asyncio.Lock()
        self.requests = {}

    @property
    def emit(self):
        return self.systemd.emit

    @emit.setter
    def emit(self, emit):
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

        async def validate(work):
            try:
                await self.validate_registration(work)
            except SelfManagementError:
                raise
            except Exception:
                pass  # Missing/unreachable registered units remain observable.

        await asyncio.gather(*(validate(work) for work in self.systemd.config.workloads.values()))

    async def validate_registration(self, work):
        if work["backend"] == "systemd":
            await self.systemd.validate_registration(work)

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
        path = docker_reference(work)["socket"]
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
        info = await api.resolve(docker_reference(work))
        if info is None:
            raise RuntimeError("Container not found; no container was created")
        return api, info

    async def operate(self, work, action, lease=None, cancel=None):
        """False means Cancel prevented dispatch; issued requests are always drained."""
        self.requested(work)
        if work["backend"] == "systemd":
            return await self.systemd.operate(work, action, lease, cancel)
        api, info = await self.resolved(work)
        if cancel is not None and cancel.is_set():
            return False
        return await api.operate(info, action)

    async def observe_docker(self, work):
        api = await self.api(work)
        info = await api.resolve(docker_reference(work))
        return await api.observe(work, info) if info else observation(state="not_found")

    async def observe(self, work):
        async def http_observation():
            settings = work.get("observer", {}).get("http")
            if not settings:
                return None
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=settings["timeout_ms"] / 1000)
                ) as session:
                    async with session.get(settings["url"], allow_redirects=False) as response:
                        return dict(
                            state="ready"
                            if response.status == settings["expected_status"]
                            else "not_ready",
                            status=response.status,
                            error=None,
                        )
            except Exception as error:
                return dict(state="unknown", status=None, error=str(error) or type(error).__name__)

        result, http = await asyncio.gather(self.observe_primary(work), http_observation())
        result["http"] = http
        return result

    async def observe_primary(self, work):
        if work["backend"] == "docker":
            return await self.observe_docker(work)

        async def lifecycle():
            try:
                return await asyncio.wait_for(self.systemd.observe(work), 3)
            except Exception as error:
                return observation(error=str(error))

        async def internal():
            try:
                return await asyncio.wait_for(self.observe_internal(work), 3)
            except Exception as error:
                message = str(error) or type(error).__name__
                return observation(observer_error=message, children_error=message)

        # Independent deadlines keep a disconnected observer from hiding systemd state.
        result, observed = await asyncio.gather(lifecycle(), internal())
        if observed is not None:
            result["children"] = observed.pop("children")
            for field in ("children_error", "observer_error", "controller"):
                result[field] = observed[field]
            if docker_reference(work):
                result["internal"] = observed
                result["health"] = observed["health"]
                result["readiness"] = observed["readiness"]
        return result

    async def observe_internal(self, work):
        if docker_reference(work):
            return await self.observe_docker(work)
        controller = work.get("controller")
        if not controller:
            return None
        reply = await host_rpc(controller, {"command": "snapshot"})
        result = observation(
            controller=bridge_info(reply) if controller["kind"] == "roslaunch" else None,
        )
        try:
            result["children"] = controller_children(
                "host:" + work["scope"] + ":" + work["unit"],
                controller,
                reply,
                namespace="host supervisor",
            )
        except Exception as error:
            result["children_error"] = result["observer_error"] = str(error)
        return result

    async def operate_child(self, work, child_id, expected_epoch, action, cancel=None):
        controller = work.get("controller")
        if not controller:
            raise ValueError("No controller configured; OS processes are observation-only")
        if docker_reference(work):
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
        if cancel is not None and cancel.is_set():
            return False
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
