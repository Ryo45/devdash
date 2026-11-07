"""One-shot RPC client, not a daemon or supervisor. Executed without a shell.

supervisord already requires Python. No package/agent is installed in a container.
The existing controller remains the sole process lifecycle owner.
"""

import http.client
import json
import socket
import sys
import xmlrpc.client


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(55)
        self.sock.connect(self.host)


class UnixTransport(xmlrpc.client.Transport):
    def __init__(self, path):
        super().__init__()
        self.path = path

    def make_connection(self, host):
        return UnixHTTP(self.path)


def main():
    kind, path, raw = sys.argv[1:]
    request = json.loads(raw)
    if kind == "supervisord":
        proxy = xmlrpc.client.ServerProxy("http://localhost", transport=UnixTransport(path))
        owner = proxy.supervisor.getPID()
        processes = proxy.supervisor.getAllProcessInfo()
        epoch = str(owner)
        if request["command"] == "snapshot":
            return {"epoch": epoch, "processes": processes}
        if request.get("epoch") != epoch:
            raise ValueError("supervisord changed; refresh before operating")
        name = request["id"]
        allowed = {p["group"] + ":" + p["name"] for p in processes}
        if name not in allowed or "*" in name:
            raise ValueError("unknown logical service; group/wildcard operations are not allowed")
        action = request["action"]
        if action not in ("start", "stop", "restart"):
            raise ValueError("unsupported action")

        def verify_owner():
            if proxy.supervisor.getPID() != owner:
                raise ValueError(
                    "supervisord changed during operation; subsequent operation was not issued"
                )

        if action in ("stop", "restart"):
            verify_owner()
            try:
                proxy.supervisor.stopProcess(name, True)
            except xmlrpc.client.Fault as error:
                if error.faultCode != 70:  # NOT_RUNNING is an idempotent stop.
                    raise
        if action in ("start", "restart"):
            verify_owner()
            proxy.supervisor.startProcess(name, True)
        return {"epoch": epoch, "accepted": True}
    if kind == "roslaunch":
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(55)
            client.connect(path)
            with client.makefile("rb") as stream:

                def exchange(payload, compatible=False):
                    client.sendall(json.dumps(dict(payload, protocol=1)).encode() + b"\n")
                    response = json.loads(stream.readline(2 * 1024 * 1024))
                    if not response.get("ok"):
                        raise ValueError(response.get("error", "bridge error"))
                    data = response["data"]
                    info = data.get("bridge", {})
                    if compatible and (
                        type(info.get("protocol")) is not int or info["protocol"] != 1
                    ):
                        raise ValueError(
                            f"Incompatible ROS Bridge IPC: expected 1, received {info.get('protocol', 'missing')}; operations disabled"
                        )
                    return data

                if request["command"] == "operate":
                    # Same connection for compatibility/epoch check and mutation.
                    snapshot = exchange({"command": "snapshot"}, compatible=True)
                    child = next(
                        (child for child in snapshot["children"] if child["id"] == request["id"]),
                        None,
                    )
                    if child is None or child["epoch"] != request.get("epoch"):
                        raise ValueError("Bridge changed; refresh before operating")
                return exchange(request, compatible=request["command"] == "operate")
    raise ValueError("unknown controller kind")


if __name__ == "__main__":
    try:
        print(json.dumps({"ok": True, "data": main()}))
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        sys.exit(1)
