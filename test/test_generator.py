from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
from types import ModuleType

import pytest
from google.protobuf import empty_pb2
from protobuf.plugin import run
from protobuf.wkt import CodeGeneratorRequest, CodeGeneratorResponse, FileDescriptorProto
from protoc_gen_connectrpc import Options, _generate
from pyqwest import Client, SyncClient
from pyqwest.testing import ASGITransport, WSGITransport

from connectrpc.client import ResponseMetadata
from connectrpc.protocol import ProtocolType

from .connectrpc.example.haberdasher_pb import Hat, Size
from .google_compat.connectrpc.example import haberdasher_pb2


def _generated_module(parameter: str) -> tuple[str, str]:
    request = CodeGeneratorRequest(
        parameter=parameter,
        file_to_generate=[haberdasher_pb2.DESCRIPTOR.name],
        proto_file=[
            FileDescriptorProto.from_binary(empty_pb2.DESCRIPTOR.serialized_pb),
            FileDescriptorProto.from_binary(haberdasher_pb2.DESCRIPTOR.serialized_pb),
        ],
    )
    output = io.BytesIO()
    run(
        "protoc-gen-connectrpc-py",
        "0.12.1",
        Options,
        _generate,
        args=[],
        stdin=io.BytesIO(request.to_binary()),
        stdout=output,
    )
    response = CodeGeneratorResponse.from_binary(output.getvalue())
    assert not response.error, response.error
    [generated] = response.file
    package = (
        "test.google_compat.connectrpc.example"
        if "protobuf=google" in parameter
        else "test.connectrpc.example"
    )
    return f"{package}.generated_contract", generated.content


_SERVER_ONLY_SCENARIO = r'''
import asyncio
import builtins
import io
import json
import sys
from types import ModuleType

# Block imports even when the native transport is installed on the test host.
original_import = builtins.__import__
def server_import(name, *args, **kwargs):
    if name == "pyqwest" or name.startswith("pyqwest.") or name == "connectrpc.client":
        raise ModuleNotFoundError("native client unavailable in server deployment")
    return original_import(name, *args, **kwargs)
builtins.__import__ = server_import

name, content, mode = json.load(sys.stdin)
module = ModuleType(name)
module.__package__ = name.rpartition(".")[0]
sys.modules[name] = module
exec(compile(content, name, "exec"), module.__dict__)
assert not hasattr(module, "HaberdasherClient")
assert not hasattr(module, "HaberdasherClientSync")
Size, Hat = module.Size, module.Hat
request = Size(inches=12)
body = request.SerializeToString() if hasattr(request, "SerializeToString") else request.to_binary()
def check_response(body):
    response = Hat.FromString(body) if hasattr(Hat, "FromString") else Hat.from_binary(body)
    assert response.size == 12
    assert response.color == "generated-server"

if mode != "sync":
    class Service(module.Haberdasher):
        async def make_hat(self, request, ctx):
            return Hat(size=request.inches, color="generated-server")
    app = module.HaberdasherASGIApplication(Service(), compressions=[])
    async def exercise_asgi():
        events = []
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        async def send(event):
            events.append(event)
        await app({
            "type": "http", "method": "POST", "scheme": "http",
            "path": app.path + "/MakeHat", "query_string": b"",
            "headers": [(b"content-type", b"application/proto"),
                        (b"connect-protocol-version", b"1")],
            "server": ("localhost", 80), "client": ("localhost", 1234),
            "http_version": "1.1", "root_path": "",
        }, receive, send)
        assert events[0]["status"] == 200, events
        check_response(b"".join(event.get("body", b"") for event in events))
    asyncio.run(exercise_asgi())
else:
    assert not hasattr(module, "HaberdasherASGIApplication")

if mode != "async":
    class ServiceSync(module.HaberdasherSync):
        def make_hat(self, request, ctx):
            return Hat(size=request.inches, color="generated-server")
    app = module.HaberdasherWSGIApplication(ServiceSync(), compressions=[])
    statuses = []
    def start_response(status, headers):
        statuses.append(status)
    result = app({
        "REQUEST_METHOD": "POST", "PATH_INFO": app.path + "/MakeHat",
        "QUERY_STRING": "", "CONTENT_TYPE": "application/proto",
        "CONTENT_LENGTH": str(len(body)), "HTTP_CONNECT_PROTOCOL_VERSION": "1",
        "HTTP_HOST": "localhost", "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body), "wsgi.errors": sys.stderr,
        "SCRIPT_NAME": "", "SERVER_NAME": "localhost", "SERVER_PORT": "80",
    }, start_response)
    check_response(b"".join(result))
    assert statuses == ["200 OK"], statuses
else:
    assert not hasattr(module, "HaberdasherWSGIApplication")
'''


@pytest.mark.parametrize("protobuf", ["py", "google"])
@pytest.mark.parametrize("mode", ["both", "async", "sync"])
def test_server_target_runs_without_native_client(protobuf: str, mode: str) -> None:
    parameter = f"target=server,protobuf={protobuf}"
    if mode != "both":
        parameter += f",io={mode}"
    name, content = _generated_module(parameter)
    result = subprocess.run(
        [sys.executable, "-c", _SERVER_ONLY_SCENARIO],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        input=json.dumps([name, content, mode]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("target", ["", "target=both"])
def test_default_target_native_clients_preserve_grpc_web_metadata(target: str) -> None:
    name, content = _generated_module(target)
    module = ModuleType(name)
    module.__package__ = name.rpartition(".")[0]
    exec(compile(content, name, "exec"), module.__dict__)

    def make_hat(request, ctx):
        ctx.response_trailers.add("x-color", "red")
        ctx.response_trailers.add("x-color", "blue")
        return Hat(size=request.inches, color="native-client")

    class Service(module.Haberdasher):
        async def make_hat(self, request, ctx):
            return make_hat(request, ctx)

    class ServiceSync(module.HaberdasherSync):
        def make_hat(self, request, ctx):
            return make_hat(request, ctx)

    async def exercise_async():
        transport = ASGITransport(module.HaberdasherASGIApplication(Service()))
        async with module.HaberdasherClient(
            "http://localhost",
            http_client=Client(transport=transport),
            protocol=ProtocolType.GRPC_WEB,
        ) as client:
            with ResponseMetadata() as metadata:
                response = await client.make_hat(Size(inches=13))
            assert response == Hat(size=13, color="native-client")
            assert metadata.trailers.getall("x-color") == ["red", "blue"]
            assert metadata.trailers["grpc-status"] == "0"

    asyncio.run(exercise_async())
    transport = WSGITransport(module.HaberdasherWSGIApplication(ServiceSync()))
    with module.HaberdasherClientSync(
        "http://localhost",
        http_client=SyncClient(transport=transport),
        protocol=ProtocolType.GRPC_WEB,
    ) as client:
        with ResponseMetadata() as metadata:
            response = client.make_hat(Size(inches=14))
        assert response == Hat(size=14, color="native-client")
        assert metadata.trailers.getall("x-color") == ["red", "blue"]
        assert metadata.trailers["grpc-status"] == "0"
