#  Copyright Contributors to the OpenCue Project
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""Pure-Python gRPC-over-HTTP/2 transport using h2 and protobuf."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from collections import deque
import enum
import gzip
import socket
import struct
import threading
import time
from urllib.parse import unquote

from google.protobuf import symbol_database
import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions


class StatusCode(enum.IntEnum):
    """Standard gRPC status codes."""
    OK = 0
    CANCELLED = 1
    UNKNOWN = 2
    INVALID_ARGUMENT = 3
    DEADLINE_EXCEEDED = 4
    NOT_FOUND = 5
    ALREADY_EXISTS = 6
    PERMISSION_DENIED = 7
    RESOURCE_EXHAUSTED = 8
    FAILED_PRECONDITION = 9
    ABORTED = 10
    OUT_OF_RANGE = 11
    UNIMPLEMENTED = 12
    INTERNAL = 13
    UNAVAILABLE = 14
    DATA_LOSS = 15
    UNAUTHENTICATED = 16

    @classmethod
    def from_int(cls, value):
        """Returns the corresponding StatusCode enum for the integer value."""
        try:
            return cls(int(value))
        except (ValueError, TypeError):
            return cls.UNKNOWN


class RpcError(Exception):
    """Exception raised for gRPC-over-HTTP/2 call failures."""

    def __init__(self, code=StatusCode.UNKNOWN, details=""):
        super(RpcError, self).__init__(details or str(code))
        self._code = code
        self._details = details

    def code(self):
        """Returns the status code of the RPC error."""
        return self._code

    def details(self):
        """Returns the detail message of the RPC error."""
        return self._details

    def __str__(self):
        code_str = self._code.name if hasattr(self._code, "name") else str(self._code)
        if self._details:
            return "%s: %s" % (code_str, self._details)
        return code_str


class ClientCallDetails(object):
    """Describes an outgoing RPC call for client interceptors."""

    def __init__(self, method, timeout=None, metadata=None, credentials=None):
        self.method = method
        self.timeout = timeout
        self.metadata = metadata
        self.credentials = credentials


class UnaryUnaryClientInterceptor(object):
    """Base class for unary-unary client interceptors."""

    def intercept_unary_unary(self, continuation, client_call_details, request):
        """Intercepts a unary-unary RPC call before invocation."""
        return continuation(client_call_details, request)


def _parse_target(target, default_port=8443):
    for prefix in ("dns:///", "ipv4:///", "ipv6:///"):
        if target.startswith(prefix):
            target = target[len(prefix):]
            break
    if target.startswith("["):
        end = target.find("]")
        if end != -1:
            host = target[1:end]
            rest = target[end + 1:]
            if rest.startswith(":"):
                return host, int(rest[1:])
            return host, default_port
    if ":" in target:
        host, port_str = target.rsplit(":", 1)
        return host, int(port_str)
    return target, default_port


def _format_grpc_timeout(timeout):
    if timeout is None:
        return None
    timeout = float(timeout)
    if timeout <= 0:
        return "1n"
    if timeout < 1000:
        ms = max(1, int(round(timeout * 1000.0)))
        return "%dm" % min(99999999, ms)
    secs = min(99999999, int(round(timeout)))
    return "%dS" % secs


def _frame_message(payload):
    return struct.pack(">BI", 0, len(payload)) + payload


def _unframe_message(data, encoding=None, max_length=None):
    if len(data) < 5:
        raise RpcError(StatusCode.INTERNAL, "Incomplete gRPC frame (%d bytes)" % len(data))
    compressed, length = struct.unpack(">BI", data[:5])
    if max_length and (0 < max_length < length):
        raise RpcError(
            StatusCode.RESOURCE_EXHAUSTED,
            "Received message larger than max (%d vs. %d)" % (length, max_length),
        )
    if len(data) < 5 + length:
        raise RpcError(
            StatusCode.INTERNAL,
            "Truncated gRPC message (expected %d bytes, got %d)" % (length, len(data) - 5),
        )
    payload = data[5:5 + length]
    if compressed == 1:
        if encoding == "gzip":
            payload = gzip.decompress(payload)
        else:
            raise RpcError(StatusCode.UNIMPLEMENTED, "Unsupported compression: %s" % encoding)
    elif compressed != 0:
        raise RpcError(StatusCode.INTERNAL, "Invalid compression flag: %d" % compressed)
    return payload


def _decode_headers(header_pairs):
    result = {}
    for name, value in header_pairs:
        key = name.decode("latin-1").lower() if isinstance(name, bytes) else str(name).lower()
        val = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        result[key] = val
    return result


def _http_status_to_grpc(http_status):
    mapping = {
        400: StatusCode.INTERNAL,
        401: StatusCode.UNAUTHENTICATED,
        403: StatusCode.PERMISSION_DENIED,
        404: StatusCode.UNIMPLEMENTED,
        429: StatusCode.UNAVAILABLE,
        502: StatusCode.UNAVAILABLE,
        503: StatusCode.UNAVAILABLE,
        504: StatusCode.UNAVAILABLE,
    }
    return mapping.get(http_status, StatusCode.UNKNOWN)


class _StreamResetBeforeResponse(Exception):
    """Internal error when a pooled connection fails before receiving response data."""


class _Http2Connection(object):
    """Single synchronous HTTP/2 connection managed via h2."""

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.closed = False
        self._sock = socket.create_connection((host, port), timeout=timeout)
        try:
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        config = h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
        self._conn = h2.connection.H2Connection(config=config)
        self._conn.initiate_connection()
        self._flush()

    def _flush(self):
        out = self._conn.data_to_send()
        if out:
            self._sock.sendall(out)

    def _remaining_timeout(self, deadline):
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RpcError(StatusCode.DEADLINE_EXCEEDED, "Deadline exceeded")
        return remaining

    def _read_events(self, deadline):
        """Reads incoming socket data and returns decoded H2 events."""
        self._sock.settimeout(self._remaining_timeout(deadline))
        if deadline is not None and (0 < deadline < time.monotonic()):
            raise RpcError(StatusCode.DEADLINE_EXCEEDED, "Deadline exceeded")
        try:
            raw = self._sock.recv(65536)
        except socket.timeout:
            raise RpcError(StatusCode.DEADLINE_EXCEEDED, "Deadline exceeded")
        if not raw:
            self.closed = True
            raise EOFError("Connection closed by peer")
        events = self._conn.receive_data(raw)
        self._flush()
        return events

    def call(self, method_path, payload, timeout=None, max_send_length=None, max_recv_length=None,
             reused=False):
        """Executes an RPC call over HTTP/2."""
        if max_send_length and (0 < max_send_length < len(payload)):
            raise RpcError(
                StatusCode.RESOURCE_EXHAUSTED,
                "Sent message larger than max (%d vs. %d)" % (len(payload), max_send_length),
            )

        deadline = (time.monotonic() + float(timeout)) if timeout is not None else None
        framed = _frame_message(payload)
        stream_id = self._conn.get_next_available_stream_id()

        authority = "%s:%d" % (self.host, self.port)
        if ":" in self.host and not self.host.startswith("["):
            authority = "[%s]:%d" % (self.host, self.port)

        headers = [
            (":method", "POST"),
            (":path", method_path),
            (":scheme", "http"),
            (":authority", authority),
            ("content-type", "application/grpc"),
            ("te", "trailers"),
            ("user-agent", "opencue-python-http2/1.0"),
        ]
        timeout_hdr = _format_grpc_timeout(timeout)
        if timeout_hdr:
            headers.append(("grpc-timeout", timeout_hdr))

        initial_headers = {}
        trailing_headers = {}
        received_any = False
        data_chunks = []
        stream_ended = False
        stream_reset = None

        def _process_events(events):
            nonlocal stream_ended, stream_reset, received_any
            for event in events:
                if isinstance(event, h2.events.ResponseReceived) and event.stream_id == stream_id:
                    received_any = True
                    initial_headers.update(_decode_headers(event.headers))
                elif isinstance(event, h2.events.DataReceived) and event.stream_id == stream_id:
                    received_any = True
                    if event.data:
                        data_chunks.append(event.data)
                    if event.flow_controlled_length:
                        self._conn.acknowledge_received_data(
                            event.flow_controlled_length, stream_id
                        )
                        self._flush()
                elif isinstance(event, h2.events.TrailersReceived) and event.stream_id == stream_id:
                    received_any = True
                    trailing_headers.update(_decode_headers(event.headers))
                elif isinstance(event, h2.events.StreamEnded) and event.stream_id == stream_id:
                    stream_ended = True
                elif isinstance(event, h2.events.StreamReset) and event.stream_id == stream_id:
                    stream_reset = event.error_code
                    stream_ended = True
                elif isinstance(event, h2.events.ConnectionTerminated):
                    self.closed = True
                    if not stream_ended:
                        raise RpcError(
                            StatusCode.UNAVAILABLE,
                            "HTTP/2 connection terminated (error=%s)" % event.error_code,
                        )

        try:
            self._sock.settimeout(self._remaining_timeout(deadline))
            self._conn.send_headers(stream_id, headers)
            self._flush()

            offset = 0
            total = len(framed)
            while offset < total:
                window = self._conn.local_flow_control_window(stream_id)
                while window <= 0:
                    _process_events(self._read_events(deadline))
                    if stream_ended:
                        break
                    window = self._conn.local_flow_control_window(stream_id)
                if stream_ended:
                    break
                chunk_size = min(window, self._conn.max_outbound_frame_size, total - offset)
                chunk = framed[offset:offset + chunk_size]
                end = (offset + chunk_size) >= total
                self._conn.send_data(stream_id, chunk, end_stream=end)
                self._flush()
                offset += chunk_size

            if total == 0 and not stream_ended:
                self._conn.end_stream(stream_id)
                self._flush()

            while not stream_ended:
                _process_events(self._read_events(deadline))
        except (EOFError, BrokenPipeError, ConnectionResetError, h2.exceptions.H2Error) as exc:
            self.closed = True
            if reused and not received_any:
                raise _StreamResetBeforeResponse(str(exc))
            raise RpcError(StatusCode.UNAVAILABLE, str(exc))
        except socket.timeout:
            self.closed = True
            raise RpcError(StatusCode.DEADLINE_EXCEEDED, "Deadline exceeded")
        except OSError as exc:
            self.closed = True
            if reused and not received_any:
                raise _StreamResetBeforeResponse(str(exc))
            raise RpcError(StatusCode.UNAVAILABLE, str(exc))

        if (stream_reset is not None and not trailing_headers
                and "grpc-status" not in initial_headers):
            if stream_reset == h2.errors.ErrorCodes.CANCEL:
                raise RpcError(StatusCode.CANCELLED, "Stream cancelled")
            if stream_reset == h2.errors.ErrorCodes.REFUSED_STREAM:
                if reused and not received_any:
                    raise _StreamResetBeforeResponse("Stream refused")
                raise RpcError(StatusCode.UNAVAILABLE, "Stream refused")
            raise RpcError(StatusCode.INTERNAL, "HTTP/2 stream reset: %s" % stream_reset)

        raw_status = trailing_headers.get("grpc-status", initial_headers.get("grpc-status"))
        raw_message = trailing_headers.get(
            "grpc-message", initial_headers.get("grpc-message", "")
        )
        grpc_message = unquote(raw_message) if raw_message else ""

        if raw_status is None:
            http_status_str = initial_headers.get(":status")
            try:
                http_status = int(http_status_str) if http_status_str is not None else 0
            except ValueError:
                http_status = 0
            if http_status == 200:
                status_code = StatusCode.INTERNAL
                if not grpc_message:
                    grpc_message = "Missing grpc-status trailer"
            else:
                status_code = _http_status_to_grpc(http_status)
                if not grpc_message:
                    grpc_message = "HTTP status %s" % http_status_str
            raise RpcError(status_code, grpc_message)

        status_code = StatusCode.from_int(raw_status)
        if status_code != StatusCode.OK:
            raise RpcError(status_code, grpc_message)

        encoding = initial_headers.get("grpc-encoding")
        return _unframe_message(
            b"".join(data_chunks), encoding=encoding, max_length=max_recv_length
        )

    def close(self):
        """Closes the underlying socket and terminates the HTTP/2 connection."""
        if self.closed:
            return
        self.closed = True
        try:
            self._conn.close_connection()
            out = self._conn.data_to_send()
            if out:
                self._sock.settimeout(0.5)
                self._sock.sendall(out)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class _UnaryUnaryMultiCallable(object):
    """Callable representing a single unary-unary gRPC method."""

    def __init__(self, channel, method, request_serializer=None, response_deserializer=None):
        self._channel = channel
        self._method = method
        self._request_serializer = request_serializer
        self._response_deserializer = response_deserializer

    def __call__(self, request, timeout=None, metadata=None, credentials=None):
        payload = self._request_serializer(request) if self._request_serializer else request
        raw_response = self._channel._invoke(self._method, payload, timeout=timeout)
        if self._response_deserializer:
            return self._response_deserializer(raw_response)
        return raw_response


class Http2Channel(object):
    """Thread-safe pooled HTTP/2 gRPC channel."""

    def __init__(self, target, options=None, max_pool_size=16):
        self._host, self._port = _parse_target(target)
        self._max_send_length = None
        self._max_recv_length = None
        if options:
            for key, value in options:
                if key == "grpc.max_send_message_length":
                    self._max_send_length = int(value)
                elif key == "grpc.max_receive_message_length":
                    self._max_recv_length = int(value)
        self._max_pool_size = max_pool_size
        self._pool = deque()
        self._lock = threading.Lock()
        self._closed = False

    def _acquire(self, timeout=None):
        with self._lock:
            if self._closed:
                raise RpcError(StatusCode.UNAVAILABLE, "Channel is closed")
            while self._pool:
                conn = self._pool.pop()
                if not conn.closed:
                    return conn, True
        try:
            conn = _Http2Connection(self._host, self._port, timeout=timeout)
            return conn, False
        except socket.timeout:
            raise RpcError(StatusCode.DEADLINE_EXCEEDED, "Connection timed out")
        except OSError as exc:
            raise RpcError(StatusCode.UNAVAILABLE, "Failed to connect to %s:%d: %s" % (
                self._host, self._port, exc
            ))

    def _release(self, conn):
        if conn.closed:
            conn.close()
            return
        with self._lock:
            if self._closed or len(self._pool) >= self._max_pool_size:
                conn.close()
            else:
                self._pool.append(conn)

    def _invoke(self, method_path, payload, timeout=None):
        conn, reused = self._acquire(timeout=timeout)
        try:
            try:
                return conn.call(
                    method_path,
                    payload,
                    timeout=timeout,
                    max_send_length=self._max_send_length,
                    max_recv_length=self._max_recv_length,
                    reused=reused,
                )
            except _StreamResetBeforeResponse:
                conn.close()
                conn, _ = self._acquire(timeout=timeout)
                return conn.call(
                    method_path,
                    payload,
                    timeout=timeout,
                    max_send_length=self._max_send_length,
                    max_recv_length=self._max_recv_length,
                    reused=False,
                )
        finally:
            self._release(conn)

    def unary_unary(self, method, request_serializer=None, response_deserializer=None):
        """Returns a callable for a unary-unary RPC method."""
        return _UnaryUnaryMultiCallable(
            self,
            method,
            request_serializer=request_serializer,
            response_deserializer=response_deserializer,
        )

    def close(self):
        """Closes all pooled connections and marks channel as closed."""
        with self._lock:
            self._closed = True
            while self._pool:
                conn = self._pool.pop()
                conn.close()


class _InterceptedUnaryUnaryCallable(object):
    def __init__(self, method, base_callable, interceptors):
        self._method = method
        self._base_callable = base_callable
        self._interceptors = list(interceptors)

    def __call__(self, request, timeout=None, metadata=None, credentials=None):
        """Invokes the intercepted unary-unary RPC callable."""
        call_details = ClientCallDetails(
            method=self._method,
            timeout=timeout,
            metadata=metadata,
            credentials=credentials,
        )

        def _invoke_chain(idx, details, req):
            if idx >= len(self._interceptors):
                return self._base_callable(
                    req,
                    timeout=details.timeout,
                    metadata=details.metadata,
                    credentials=details.credentials,
                )
            interceptor = self._interceptors[idx]
            return interceptor.intercept_unary_unary(
                lambda next_details, next_req: _invoke_chain(idx + 1, next_details, next_req),
                details,
                req,
            )

        return _invoke_chain(0, call_details, request)


class InterceptedChannel(object):
    """Channel wrapper applying unary-unary client interceptors."""

    def __init__(self, channel, interceptors):
        self._channel = channel
        self._interceptors = list(interceptors)

    def unary_unary(self, method, request_serializer=None, response_deserializer=None):
        """Creates an intercepted unary-unary callable for the given method."""
        base_callable = self._channel.unary_unary(
            method,
            request_serializer=request_serializer,
            response_deserializer=response_deserializer,
        )
        return _InterceptedUnaryUnaryCallable(method, base_callable, self._interceptors)

    def close(self):
        """Closes the underlying channel."""
        self._channel.close()


def insecure_channel(target, options=None):
    """Creates an insecure HTTP/2 channel to the target host and port."""
    return Http2Channel(target, options=options)


def intercept_channel(channel, *interceptors):
    """Wraps an existing channel with the provided client interceptors."""
    return InterceptedChannel(channel, interceptors)


class DynamicStub(object):
    """gRPC client stub dynamically built from a protobuf ServiceDescriptor."""

    def __init__(self, channel, service_descriptor):
        sym_db = symbol_database.Default()
        for method in service_descriptor.methods:
            req_cls = sym_db.GetSymbol(method.input_type.full_name)
            resp_cls = sym_db.GetSymbol(method.output_type.full_name)
            path = "/%s/%s" % (service_descriptor.full_name, method.name)
            callable_method = channel.unary_unary(
                path,
                request_serializer=req_cls.SerializeToString,
                response_deserializer=resp_cls.FromString,
            )
            setattr(self, method.name, callable_method)


def stub_factory(pb2_module, service_name):
    """Returns a stub constructor taking (channel) for the given protobuf service."""
    service_descriptor = pb2_module.DESCRIPTOR.services_by_name[service_name]

    def _create_stub(channel):
        return DynamicStub(channel, service_descriptor)

    _create_stub.__name__ = "%sStub" % service_name
    return _create_stub
