#!/usr/bin/env python3
"""Runs inside a ROS 2 environment; delegates all child lifecycle to LaunchService."""
import asyncio
import json
from launch import LaunchDescription, LaunchService
from launch.actions import ExecuteProcess, RegisterEventHandler
from launch.event_handlers import OnProcessStart, OnProcessExit
from launch.events import Shutdown, matches_action, IncludeLaunchDescription as IncludeEvent
from launch.events.process import ShutdownProcess

async def probe():
    service = LaunchService(noninteractive=True)
    original = ExecuteProcess(cmd=['/usr/bin/sleep', '60'], respawn=True,
                              respawn_delay=.1, sigterm_timeout='0.2', sigkill_timeout='0.2')
    started, exited, targeted = [], [], []
    def on_start(event, context): started.append((event.action, event.pid))
    def on_exit(event, context): exited.append((event.action, event.returncode))
    def original_exit(event, context): targeted.append(event.action)
    service.include_launch_description(LaunchDescription([
        RegisterEventHandler(OnProcessStart(on_start=on_start)),
        RegisterEventHandler(OnProcessExit(on_exit=on_exit)),
        RegisterEventHandler(OnProcessExit(target_action=original, on_exit=original_exit)), original]))
    task = asyncio.create_task(service.run_async(shutdown_when_idle=False))
    async def until(predicate):
        async def poll():
            while not predicate(): await asyncio.sleep(.01)
        await asyncio.wait_for(poll(), 5)
    try:
        await until(lambda: len(started)==1)
        await service.context.emit_event(ShutdownProcess(process_matcher=matches_action(original)))
        await until(lambda: len(exited)==1)
        await asyncio.sleep(.3)
        assert len(started)==1, 'native Stop must suppress respawn'
        try:
            original.execute(service.context)
        except RuntimeError as error:
            assert 'executed more than once' in str(error)
            same_action_error = str(error)
        else:
            raise AssertionError('expected the single-use Action restriction')
        replacement = ExecuteProcess(cmd=['/usr/bin/sleep','60'])
        await service.context.emit_event(IncludeEvent(LaunchDescription([replacement])))
        await until(lambda: len(started)==2)
        assert started[0][1] != started[1][1]
        await service.context.emit_event(ShutdownProcess(process_matcher=matches_action(replacement)))
        await until(lambda: len(exited)==2)
        assert len(targeted)==1, 'original Action identity handler must not attach to replacement'
        result = {'native_stop_suppresses_respawn': True,
                  'same_action_restart_supported': False, 'same_action_error': same_action_error,
                  'fresh_action_can_start': True, 'pid_changed': True,
                  'original_targeted_handlers_transfer_to_fresh_action': False}
        print('DEVDASH_PROBE ' + json.dumps(result), flush=True)
    finally:
        if not task.done(): await service.context.emit_event(Shutdown(reason='devdash isolated probe complete'))
        await asyncio.wait_for(task, 10)

asyncio.run(probe())
