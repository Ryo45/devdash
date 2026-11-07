"""Job tracking races exercised without real systemd or privileged services."""

import asyncio

import pytest
from dbus_fast import Message, MessageFlag, MessageType

from devdash.adapters.systemd import MANAGER, PATH, SERVICE, BusError, Session


class Backend:
    revision = 0

    def __init__(self):
        self.events = []
        self.config = type(
            "Config",
            (),
            {"workloads": {"a": {"backend": "systemd", "scope": "user", "unit": "a.service"}}},
        )()

    def event(self, *args, **kwargs):
        self.events.append((args, kwargs))


def removed(session, path, result="done", sender=":1.42"):
    session.message(
        Message(
            message_type=MessageType.SIGNAL,
            sender=sender,
            path=PATH,
            interface=MANAGER,
            member="JobRemoved",
            signature="uoss",
            body=[1, path, "a.service", result],
        )
    )


@pytest.mark.parametrize("early", [True, False])
@pytest.mark.parametrize("result", ["done", "failed"])
async def test_job_completion_before_and_after_reply(early, result):
    backend = Backend()
    session = Session(backend, "user", 1)
    session.owner = ":1.42"

    async def unit(name):
        return "/unit/a", {"Id": name}

    async def call(*args, **kwargs):
        if args[0] == "GetAll":
            return [{"ExecStart": []}]
        if early:
            removed(session, "/job/1", result)
        else:
            asyncio.get_running_loop().call_later(0.01, removed, session, "/job/1", result)
        return ["/job/1"]

    session.unit, session.call = unit, call
    if result == "done":
        await session.operate({"unit": "a.service"}, "start", session.lost)
    else:
        with pytest.raises(RuntimeError, match="job result failed"):
            await session.operate({"unit": "a.service"}, "start", session.lost)
    assert backend.events[-1][1]["correlation"] == "requested"
    assert not session.jobs and not session.early and not session.issuing


async def test_owner_loss_terminates_wait_without_replay():
    backend = Backend()
    session = Session(backend, "user", 1)
    session.owner, session.published = ":1.42", True
    calls = []

    async def unit(name):
        return "/unit/a", {"Id": name}

    async def call(*args, **kwargs):
        if args[0] == "GetAll":
            return [{"ExecStart": []}]
        calls.append(args[0])
        return ["/job/1"]

    session.unit, session.call = unit, call
    task = asyncio.create_task(session.operate({"unit": "a.service"}, "start", session.lost))
    while not session.jobs:
        await asyncio.sleep(0)
    removed(session, "/job/1", sender=":1.99")
    assert not task.done()
    session.message(
        Message(
            message_type=MessageType.SIGNAL,
            sender="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="NameOwnerChanged",
            signature="sss",
            body=[SERVICE, ":1.42", ":1.99"],
        )
    )
    with pytest.raises(RuntimeError, match="outcome unknown"):
        await task
    assert calls == ["StartUnit"]
    assert session.lost.is_set()


async def test_polkit_flag_and_error_message():
    session = Session(Backend(), "system", 1)
    session.owner = ":1.42"

    class Bus:
        async def call(self, message):
            assert message.flags & MessageFlag.ALLOW_INTERACTIVE_AUTHORIZATION
            return Message(
                message_type=MessageType.ERROR,
                reply_serial=1,
                error_name="org.freedesktop.DBus.Error.AccessDenied",
                signature="s",
                body=["denied"],
            )

    session.bus = Bus()
    with pytest.raises(BusError, match="Polkit"):
        await session.call("StartUnit", "ss", ["a.service", "replace"], interactive=True)


async def test_unrelated_jobs_during_pending_method_are_flushed_after_reply():
    backend = Backend()
    session = Session(backend, "user", 1)
    session.owner = ":1.42"

    async def unit(name):
        return "/unit/a", {"Id": name}

    async def call(*args, **kwargs):
        if args[0] == "GetAll":
            return [{"ExecStart": []}]
        removed(session, "/job/other")
        removed(session, "/job/ours")
        assert not backend.events
        return ["/job/ours"]

    session.unit, session.call = unit, call
    await session.operate({"unit": "a.service"}, "start", session.lost)
    correlations = {facts["job_path"]: facts["correlation"] for _, facts in backend.events}
    assert correlations == {"/job/ours": "requested", "/job/other": "unrelated"}
    assert not session.jobs and not session.early


async def test_overflow_marks_tracking_unknown_without_silently_losing_owned_jobs():
    backend = Backend()
    session = Session(backend, "user", 1)
    session.owner, session.published = ":1.42", True
    session.issuing["a.service"] = 1
    future = asyncio.get_running_loop().create_future()
    session.jobs["/job/owned"] = ("a.service", future)
    wait = asyncio.create_task(session.guarded(future))
    for index in range(257):
        removed(session, f"/job/{index}")
    assert session.lost.is_set() and "overflow" in session.reason
    with pytest.raises(RuntimeError, match="outcome unknown"):
        await wait
    assert "/job/owned" in session.jobs
    assert len(session.early) == 256
