"""Explicit, opt-in launch definitions for reloadable composable nodes.

The existing ComposableNode description remains the definition of record.
No private fields or Action execution state are copied/reset.
"""
import asyncio
from launch import Action
from launch_ros.actions import ComposableNodeContainer
from launch_ros.descriptions import ComposableNode
from launch_ros.actions.load_composable_nodes import get_composable_node_load_request

class ManagedComponents(Action):
    def __init__(self, *, container, nodes, **kwargs):
        super().__init__(**kwargs)
        if not isinstance(container, ComposableNodeContainer):
            raise TypeError('ManagedComponents requires its actual ComposableNodeContainer Action')
        if not nodes or any(not key or type(node) is not ComposableNode for key, node in nodes.items()):
            raise ValueError('Use named ordinary ComposableNode descriptions; lifecycle/custom descriptions are unsupported')
        self.container = container
        self.nodes = nodes
        self.future = None

    def execute(self, context):
        from .bridge import ACTIVE
        registry = ACTIVE.get()
        if registry is None:
            raise RuntimeError('ManagedComponents requires the devdash-roslaunch wrapper')
        # Same official Jazzy evaluator used by LoadComposableNodes, evaluated
        # while this launch context (namespace/remaps/parameters) is still valid.
        entries = [(key, get_composable_node_load_request(node, context)) for key, node in self.nodes.items()]
        entries = [(key, request) for key, request in entries if request is not None]
        if registry.components is None:
            from .components import Components
            registry.components = Components(registry)
        self.future = asyncio.create_task(registry.components.define(self.container, entries))

    def get_asyncio_future(self):
        return self.future
