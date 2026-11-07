"""Observe native launch events and delegate stop to ShutdownProcess.

Standard ExecuteLocal Actions are single-use. Start/restart are deliberately
unsupported instead of cloning Actions, resetting private fields or signalling
PIDs. Existing respawn/on_exit behavior stays with launch.
"""
import argparse
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import uuid

from . import IPC_PROTOCOL, __version__, identity

ACTIVE = ContextVar("devdash_launch_registry", default=None)

@dataclass
class Process:
    action: object
    child: dict
    exited: asyncio.Event
    generation: int = 0

class Registry:
    def __init__(self):
        self.epoch = uuid.uuid4().hex
        self.sequence = 0
        self.processes = {}
        self.by_action = {}
        self.condition = asyncio.Condition()
        self.launch_service = None
        self.components = None

    def changed(self):
        self.sequence += 1
        async def notify():
            async with self.condition:
                self.condition.notify_all()
        asyncio.create_task(notify())

    def started(self, event, _context):
        key = self.by_action.get(event.action)
        if key is None:
            key = 'launch:' + str(len(self.processes) + 1)
            self.by_action[event.action] = key
            self.processes[key] = Process(event.action, {}, asyncio.Event())
        record = self.processes[key]
        record.exited.clear()
        record.generation += 1
        record.child = dict(id=key, epoch=self.epoch, name=event.process_name, kind='service',
            state='running', pid=event.pid, pid_namespace='bridge process namespace',
            cpu_percent=None, memory_bytes=None, exit_code=None, actions=['stop'],
            reason='Native launch Action; Stop may trigger on_exit handlers. Start/Restart unavailable: Actions are single-use; the original definition cannot safely be replayed.')
        from launch_ros.actions import ComposableNodeContainer
        if isinstance(event.action, ComposableNodeContainer):
            if self.components is None:
                from .components import Components
                self.components = Components(self)
            self.components.owner_started(event.action)
        self.changed()

    def exited(self, event, _context):
        key = self.by_action.get(event.action)
        if key is None:
            return
        record = self.processes[key]
        record.child.update(state='exited', pid=None, exit_code=event.returncode, actions=[])
        record.exited.set()
        self.changed()

    def snapshot(self):
        return {'bridge': identity(), 'epoch': self.epoch, 'sequence': self.sequence,
                'children': [dict(p.child) for p in self.processes.values()] + (self.components.snapshot() if self.components is not None else [])}

    async def operate(self, request):
        if request.get('id', '').startswith(('component:', 'component-observed:')):
            if self.components is None:
                raise ValueError('No ComponentManager registered')
            return await self.components.operate(request)
        if request.get('epoch') != self.epoch:
            raise ValueError('Bridge changed; refresh before operating')
        record = self.processes.get(request.get('id'))
        if record is None:
            raise ValueError('Unknown launch Action')
        if request.get('action') != 'stop':
            raise ValueError('Start/Restart unsupported: native launch Actions cannot be executed twice')
        if 'stop' not in record.child['actions']:
            raise ValueError('Process is not running or is already stopping')
        from launch.events.process import ShutdownProcess
        from launch.events import matches_action
        record.child.update(state='stopping', actions=[])
        self.changed()
        # Native launch owns escalation, respawn suppression and on_exit events.
        await self.launch_service.context.emit_event(ShutdownProcess(process_matcher=matches_action(record.action)))
        await asyncio.wait_for(record.exited.wait(), timeout=60)
        return self.snapshot()

async def reply(writer, data=None, error=None):
    writer.write((json.dumps({'ok': error is None, 'data': data, 'error': error, 'bridge': identity()}) + '\n').encode())
    await writer.drain()

async def handle(registry, reader, writer):
    try:
        for _ in range(2):
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            if not raw:
                break
            request = json.loads(raw)
            command = request.get('command')
            if command == 'snapshot':
                await reply(writer, registry.snapshot())
            elif command == 'operate':
                if type(request.get('protocol')) is not int or request['protocol'] != IPC_PROTOCOL:
                    raise ValueError(f'Bridge {__version__} requires IPC {IPC_PROTOCOL}; received {request.get("protocol", "missing")}')
                await reply(writer, await registry.operate(request))
            elif command == 'watch':
                # Watch is optional; clients can also poll snapshots. No history is
                # invented after a disconnect; sequence + epoch detect the gap.
                while True:
                    snapshot = registry.snapshot()
                    await reply(writer, snapshot)
                    async with registry.condition:
                        try:
                            await asyncio.wait_for(registry.condition.wait_for(
                                lambda: registry.sequence != snapshot['sequence']), timeout=30)
                        except asyncio.TimeoutError:
                            pass
            else:
                raise ValueError('Unknown command')
    except (ConnectionError, asyncio.CancelledError):
        pass
    except Exception as error:
        try:
            await reply(writer, error=str(error))
        except ConnectionError:
            pass
    finally:
        writer.close()
        await writer.wait_closed()

async def run(args):
    from launch import LaunchDescription, LaunchService
    from launch.actions import IncludeLaunchDescription, RegisterEventHandler
    from launch.event_handlers import OnProcessExit, OnProcessStart
    from launch.launch_description_sources import AnyLaunchDescriptionSource
    registry = Registry()
    service = LaunchService(argv=args.arguments, noninteractive=True)
    registry.launch_service = service
    arguments = []
    for argument in args.arguments:
        if ':=' not in argument:
            raise ValueError('Launch arguments must have name:=value form')
        arguments.append(argument.split(':=', 1))
    service.include_launch_description(LaunchDescription([
        RegisterEventHandler(OnProcessStart(on_start=registry.started)),
        RegisterEventHandler(OnProcessExit(on_exit=registry.exited)),
        IncludeLaunchDescription(AnyLaunchDescriptionSource(str(args.launch_file)), launch_arguments=arguments),
    ]))
    path = args.socket
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise ValueError('Bridge socket path is not a socket')
        try:
            _, writer = await asyncio.open_unix_connection(path)
        except (ConnectionRefusedError, FileNotFoundError):
            path.unlink(missing_ok=True)
        else:
            writer.close()
            await writer.wait_closed()
            raise ValueError('Another bridge owns this socket')
    server = await asyncio.start_unix_server(lambda r, w: handle(registry, r, w), path, limit=1024*1024)
    os.chmod(path, 0o600)
    active = ACTIVE.set(registry)
    try:
        # Keep exited Action observations available until wrapper shutdown.
        return await service.run_async(shutdown_when_idle=False)
    finally:
        if registry.components is not None:
            await registry.components.close()
        ACTIVE.reset(active)
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=f'devdash-roslaunch {__version__} (IPC {IPC_PROTOCOL})')
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('launch_file', type=Path)
    parser.add_argument('arguments', nargs='*')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))

if __name__ == '__main__':
    main()
