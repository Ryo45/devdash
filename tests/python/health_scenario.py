"""Dedicated real user units and an existing test image; no production service changes."""

import asyncio
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import yaml

from devdash.backend import Backends
from devdash.engine import Engine
from devdash.ipc import Server, request
from devdash.model import Config

ROOT = Path(__file__).resolve().parents[2]


async def eventually(check, timeout=15):
    async def poll():
        while True:
            if value := await check():
                return value
            await asyncio.sleep(0.03)

    return await asyncio.wait_for(poll(), timeout)


class HealthScenario:
    def __init__(self, directory):
        self.directory = directory
        self.name = "devdash-py-it-" + uuid.uuid4().hex[:10]
        self.units = {key: f"{self.name}-{key}.service" for key in ("container", "next")}
        self.docker = shutil.which("docker")
        self.runtime = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user"
        self.socket = directory / "core.sock"
        self.files = []
        self.calls = []
        self.created = False
        self.listener = self.server = self.monitor = None
        self.control = directory / "control"
        self.control.mkdir()
        self.config = Config(
            {
                "workloads": {
                    "container": {
                        "backend": "systemd",
                        "unit": self.units["container"],
                        "observer": {"docker": {"container": self.name}},
                        "controller": {
                            "kind": "supervisord",
                            "socket": "/tmp/devdash-supervisor.sock",
                        },
                    },
                    "next": {"backend": "systemd", "unit": self.units["next"]},
                },
                "groups": {
                    "robot": {
                        "steps": [
                            {"workload": "container"},
                            {"gate": "operator"},
                            {"workload": "next"},
                        ]
                    }
                },
                "gates": {"operator": {"type": "manual"}},
            }
        )
        self.backend = Backends(self.config)
        self.engine = Engine(self.config, self.backend)
        operate = self.backend.operate

        async def recorded(work, action, lease=None):
            self.calls.append((work["unit"], action))
            return await operate(work, action, lease)

        self.backend.operate = recorded

    async def command_process(self, *args, check=True):
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            output, error = await asyncio.wait_for(process.communicate(), 30)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if check:
            assert process.returncode == 0, (args, error.decode())
        return output.decode().strip()

    async def start(self, health_timeout=30, unit_timeout=40, start_period="120s"):
        await self.command_process(
            self.docker,
            "create",
            "--name",
            self.name,
            "--network",
            "none",
            "--memory",
            "512m",
            "--mount",
            f"type=bind,src={self.control},dst=/control,readonly",
            "--health-cmd",
            "test -f /control/ready",
            "--health-interval",
            "1s",
            "--health-timeout",
            "1s",
            "--health-retries",
            "1",
            "--health-start-period",
            start_period,
            os.environ.get("DEVDASH_TEST_IMAGE", "devdash-supervisor-test:4.3.0"),
        )
        self.created = True
        self.runtime.mkdir(parents=True, exist_ok=True)
        for key, unit in self.units.items():
            path = self.runtime / unit
            if key == "container":
                text = (
                    "[Unit]\nDescription=devdash Health start boundary fixture\n[Service]\nType=exec\n"
                    f"ExecStart={self.docker} start --attach {self.name}\n"
                    f"ExecStartPost={sys.executable} {ROOT}/examples/systemd-docker-health/wait_health.py --timeout {health_timeout} --interval 0.1 --docker {self.docker} {self.name}\n"
                    f"ExecStop={self.docker} stop --time 1 {self.name}\n"
                    f"ExecStopPost={self.docker} stop --time 1 {self.name}\n"
                    f"TimeoutStartSec={unit_timeout}\nTimeoutStopSec=10\nRestart=no\n"
                )
            else:
                text = "[Unit]\nDescription=devdash next Step fixture\n[Service]\nType=exec\nExecStart=/bin/sleep 3600\nTimeoutStopSec=3\n"
            path.write_text(text)
            self.files.append(path)
        await self.backend.start()
        await self.backend.systemd.session("user").call("Reload")
        path = self.directory / "config.yaml"
        path.write_text(yaml.safe_dump(self.config.json()))
        self.server = Server(self.engine, path)
        self.listener = await asyncio.start_unix_server(self.server.handle, path=str(self.socket))
        await self.engine.poll()
        assert self.calls == []
        return self

    async def command(self, command, **args):
        return await request(self.socket, dict(command=command, **args))

    async def observe(self, key="container"):
        return await self.backend.observe(self.config.workloads[key])

    async def health_wait(self):
        async def waiting():
            value = await self.observe()
            assert self.engine.run["status"] != "failed", self.engine.run
            return (
                value
                if (
                    value["state"] == "activating"
                    and value["sub_state"] == "start-post"
                    and (value.get("internal") or {}).get("state") == "active"
                    and value["health"] == "starting"
                    and self.backend.systemd.session("user").jobs
                )
                else None
            )

        return await eventually(waiting)

    async def done(self):
        return await asyncio.wait_for(self.engine.wait(), 25)

    async def service_result(self):
        session = self.backend.systemd.session("user")
        path, _ = await session.unit(self.units["container"])
        return await session.properties(path, "org.freedesktop.systemd1.Service")

    def release(self):
        (self.control / "ready").touch()

    def jobs(self):
        return [
            e["job_result"]
            for e in self.engine.events
            if e["kind"] == "job"
            and e["workload"] == "container"
            and e["correlation"] == "requested"
        ]

    async def record(self, case):
        service = await self.service_result()
        _, unit = await self.backend.systemd.session("user").unit(self.units["container"])
        entry = dict(
            case=case,
            run=self.engine.run,
            calls=self.calls,
            jobs=self.jobs(),
            service_result=service["Result"],
            unit_job=unit.get("Job"),
            tracked_jobs=list(self.backend.systemd.session("user").jobs),
            observed=await self.observe(),
        )
        print(
            json.dumps({key: entry[key] for key in ("case", "jobs", "service_result")}), flush=True
        )
        if path := os.environ.get("DEVDASH_HEALTH_REPORT"):
            path = Path(path)
            entries = json.loads(path.read_text()) if path.exists() else []
            path.write_text(json.dumps([*entries, entry], indent=2))
        return entry

    async def close(self):
        if self.listener:
            self.listener.close()
            await self.listener.wait_closed()
        tasks = [
            task
            for task in [
                self.monitor,
                self.engine.task,
                *(self.server.clients if self.server else []),
            ]
            if task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.files:
            await self.command_process(
                "systemctl",
                "--user",
                "stop",
                self.units["next"],
                self.units["container"],
                check=False,
            )
            await self.command_process(
                "systemctl", "--user", "reset-failed", *self.units.values(), check=False
            )
            for path in self.files:
                path.unlink(missing_ok=True)
            await self.command_process("systemctl", "--user", "daemon-reload")
        await self.backend.close()
        if self.created:
            await self.command_process(self.docker, "rm", "-f", self.name)
