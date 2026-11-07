"""Native ComponentManager service adapter; never manages OS processes.

Unregistered components are observation-only. Managed definitions keep the
original evaluated LoadNode request, rather than reverse-engineering ROS graph
names, PIDs or plugin metadata from ListNodes.
"""
import asyncio
from dataclasses import dataclass
import threading

@dataclass
class Definition:
    owner: object
    key: str
    request: bytes
    generation: int
    unique_id: int | None = None
    state: str = 'unloaded'
    name: str = ''

class Components:
    def __init__(self, registry):
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node
        self.registry = registry
        self.context = Context()
        rclpy.init(context=self.context)
        self.node = Node('_devdash_components', context=self.context, enable_rosout=False)
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()
        self.owners = {}
        self.definitions = {}
        self.observed = {}
        self.clients = {}
        self.ready = set()
        self.lock = asyncio.Lock()
        self.task = asyncio.create_task(self.watch())

    def owner_record(self, owner):
        return self.registry.processes[self.registry.by_action[owner]]

    def epoch(self, owner):
        process = self.owner_record(owner)
        return f'{self.registry.epoch}/{process.child["id"]}/{process.generation}'

    def owner_started(self, owner):
        # These are resolved launch Action properties, not ROS graph/PID guesses.
        name = owner.node_name  # Same fully qualified property used by native LoadComposableNodes.
        self.owners[owner] = name
        self.ready.discard(owner)
        for definition in self.definitions.values():
            if definition.owner is owner:
                definition.unique_id = None
                definition.generation = self.owner_record(owner).generation
                definition.state = 'unloaded'
        self.registry.changed()

    async def call(self, owner, method, request):
        from composition_interfaces.srv import ListNodes, LoadNode, UnloadNode
        cls = {'list_nodes': ListNodes, 'load_node': LoadNode, 'unload_node': UnloadNode}[method]
        key = (owner, method)
        if key not in self.clients:
            self.clients[key] = self.node.create_client(cls, self.owners[owner] + '/_container/' + method)
        client = self.clients[key]
        if not client.service_is_ready():
            available = await asyncio.to_thread(client.wait_for_service, timeout_sec=3)
            if not available:
                raise RuntimeError(f'ComponentManager service unavailable: {method}')
        loop = asyncio.get_running_loop()
        result = loop.create_future()
        future = client.call_async(request)
        def done(future):
            def transfer():
                if result.done():
                    return
                try:
                    result.set_result(future.result())
                except Exception as error:
                    result.set_exception(error)
            loop.call_soon_threadsafe(transfer)
        future.add_done_callback(done)
        try:
            return await asyncio.wait_for(result, 10)
        except asyncio.TimeoutError:
            raise RuntimeError('ComponentManager timeout; outcome unknown; request was not retried') from None

    async def define(self, owner, entries):
        from rclpy.serialization import serialize_message
        async def wait_owner():
            while owner not in self.owners:
                await asyncio.sleep(.02)
        await asyncio.wait_for(wait_owner(), 20)
        generation = self.owner_record(owner).generation
        for logical_id, request in entries:
            key = f'component:{self.owner_record(owner).child["id"]}:{logical_id}'
            if key in self.definitions:
                raise ValueError('Duplicate managed component ID: ' + logical_id)
            definition = Definition(owner, key, serialize_message(request), generation)
            self.definitions[key] = definition
            # This first load is the explicitly authored launch Action. Later
            # process respawn only changes observations; it does not reload.
            await self.operate({'id': key, 'epoch': self.epoch(owner), 'action': 'start'})

    async def operate(self, request):
        from composition_interfaces.srv import LoadNode, UnloadNode
        from rclpy.serialization import deserialize_message
        definition = self.definitions.get(request.get('id'))
        if definition is None:
            return await self.stop_observed(request)
        owner = definition.owner
        expected = self.epoch(owner)
        if request.get('epoch') != expected:
            raise ValueError('ComponentManager generation changed; refresh before operating')
        if self.owner_record(owner).child['state'] != 'running':
            raise ValueError('ComponentManager process is not running')
        action = request.get('action')
        if action not in ('start', 'stop', 'restart'):
            raise ValueError('Unsupported component operation')
        if definition.state == 'unknown':
            raise ValueError('Previous component result is unknown; refresh/inspect before retrying')
        if self.lock.locked():
            raise ValueError('Another component operation is running')
        async with self.lock:
            try:
                if action in ('stop', 'restart') and definition.unique_id is not None:
                    definition.state = 'unloading'
                    self.registry.changed()
                    call = UnloadNode.Request()
                    call.unique_id = definition.unique_id
                    response = await self.call(owner, 'unload_node', call)
                    if not response.success:
                        raise RuntimeError(response.error_message)
                    definition.unique_id = None
                    definition.state = 'unloaded'
                if self.epoch(owner) != expected or self.owner_record(owner).child['state'] != 'running':
                    raise RuntimeError('ComponentManager changed during operation; no subsequent load issued')
                if action in ('start', 'restart'):
                    if definition.unique_id is not None:
                        return self.registry.snapshot()  # Already loaded; no duplicate load.
                    definition.state = 'loading'
                    self.registry.changed()
                    load = deserialize_message(definition.request, LoadNode.Request)
                    response = await self.call(owner, 'load_node', load)
                    if not response.success:
                        raise RuntimeError(response.error_message)
                    if self.epoch(owner) != expected:
                        raise RuntimeError('ComponentManager changed while loading; result unknown')
                    definition.unique_id = response.unique_id
                    definition.name = response.full_node_name
                    definition.state = 'loaded'
            except Exception:
                definition.state = 'unknown'
                raise
            finally:
                self.registry.changed()
        return self.registry.snapshot()

    async def stop_observed(self, request):
        from composition_interfaces.srv import UnloadNode
        for owner, nodes in self.observed.items():
            process = self.owner_record(owner)
            for unique_id in list(nodes):
                key = f'component-observed:{process.child["id"]}:{unique_id}'
                if key != request.get('id'):
                    continue
                if (request.get('epoch') != self.epoch(owner) or owner not in self.ready
                        or process.child['state'] != 'running'):
                    raise ValueError('ComponentManager changed or is unavailable; refresh')
                if request.get('action') != 'stop':
                    raise ValueError('Reload unavailable: original LoadNode request is unknown')
                if self.lock.locked():
                    raise ValueError('Another component operation is running')
                async with self.lock:
                    call = UnloadNode.Request()
                    call.unique_id = unique_id
                    response = await self.call(owner, 'unload_node', call)
                    if not response.success:
                        raise RuntimeError(response.error_message)
                    nodes.pop(unique_id, None)
                    self.registry.changed()
                return self.registry.snapshot()
        raise ValueError('Unknown component; refresh')

    def snapshot(self):
        children = []
        claimed = set()
        for definition in self.definitions.values():
            owner = definition.owner
            process = self.owner_record(owner)
            available = process.child['state'] == 'running' and owner in self.ready
            actions = []
            if available and definition.state == 'loaded':
                actions = ['stop', 'restart']
            elif available and definition.state == 'unloaded':
                actions = ['start', 'restart']
            if definition.unique_id is not None:
                claimed.add((owner, definition.unique_id))
            children.append(dict(id=definition.key, epoch=self.epoch(owner), name=definition.name or definition.key,
                parent=process.child['id'], kind='component', state=definition.state if available else 'unavailable',
                pid=None, pid_namespace='shared ComponentManager process', cpu_percent=None, memory_bytes=None,
                exit_code=None, actions=actions, runtime_id=str(definition.unique_id) if definition.unique_id is not None else None,
                reason='ComponentManager Load/Unload; evaluated original request retained. Resources are shared. Concurrent manager restart can race a ROS service request; no automatic retry.'))
        for owner, nodes in self.observed.items():
            process = self.owner_record(owner)
            for unique_id, name in nodes.items():
                if (owner, unique_id) in claimed:
                    continue
                children.append(dict(id=f'component-observed:{process.child["id"]}:{unique_id}', epoch=self.epoch(owner),
                    name=name, parent=process.child['id'], kind='component',
                    state='loaded' if owner in self.ready and process.child['state']=='running' else 'unavailable',
                    pid=None, pid_namespace='shared ComponentManager process', cpu_percent=None, memory_bytes=None,
                    exit_code=None, runtime_id=str(unique_id), actions=['stop'] if owner in self.ready and process.child['state']=='running' else [], reason='Unload only: original LoadNode request is unknown. Opt in with ManagedComponents for load/reload; resources are shared.'))
        return children

    async def watch(self):
        from composition_interfaces.srv import ListNodes
        previous = []
        while True:
            for owner in list(self.owners):
                if self.owner_record(owner).child['state'] != 'running':
                    self.ready.discard(owner)
                    continue
                generation = self.epoch(owner)
                before = {key:(d.unique_id,d.state) for key,d in self.definitions.items() if d.owner is owner}
                try:
                    response = await self.call(owner, 'list_nodes', ListNodes.Request())
                    if generation != self.epoch(owner):
                        continue
                    self.observed[owner] = dict(zip(response.unique_ids, response.full_node_names))
                    self.ready.add(owner)
                    for key, definition in self.definitions.items():
                        if (definition.owner is owner and definition.state in ('loaded','unknown')
                                and definition.unique_id is not None and not self.lock.locked()
                                and before.get(key)==(definition.unique_id,definition.state)):
                            if definition.unique_id not in self.observed[owner]:
                                definition.unique_id = None
                                definition.state = 'unloaded'
                            else:
                                definition.state = 'loaded'  # Observation, never automatic reloading.
                except Exception:
                    self.ready.discard(owner)
            current = self.snapshot()
            if current != previous:
                previous = current
                self.registry.changed()
            await asyncio.sleep(.5)

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.executor.shutdown(timeout_sec=3)
        self.thread.join(timeout=3)
        self.node.destroy_node()
        self.context.shutdown()
