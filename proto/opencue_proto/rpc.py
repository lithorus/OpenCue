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

"""gRPC servicer implementations for RQD."""

# pylint: disable=unused-argument

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from collections import deque
import enum
import logging
import socket
import struct
import threading
import time
from urllib.parse import quote, unquote
import zlib

from google.protobuf import symbol_database
import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions


log = logging.getLogger(__name__)


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


def _parse_target(target, default_port=8444):
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


def _parse_grpc_timeout(timeout_str):
    if not timeout_str:
        return None
    units = {
        "H": 3600.0,
        "M": 60.0,
        "S": 1.0,
        "m": 0.001,
        "u": 0.000001,
        "n": 0.000000001,
    }
    unit = timeout_str[-1]
    if unit in units:
        try:
            return float(timeout_str[:-1]) * units[unit]
        except ValueError:
            return None
    return None


def _frame_message(payload):
    return struct.pack(">BI", 0, len(payload)) + payload


def _decompress_gzip_bounded(payload, max_length=None):
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    buf = bytearray()
    chunk_size = 65536
    try:
        # Feed payload in slices to allow early termination if bounded
        offset = 0
        while offset < len(payload):
            chunk = payload[offset:offset + chunk_size]
            offset += len(chunk)
            max_chunk = (max_length + 1 - len(buf)) if max_length else 0
            decompressed = decompressor.decompress(chunk, max_chunk)
            buf.extend(decompressed)
            if max_length and len(buf) > max_length:
                raise RpcError(
                    StatusCode.RESOURCE_EXHAUSTED,
                    "Decompressed message larger than max (%d vs. %d)" % (len(buf), max_length),
                )
        unconsumed = decompressor.unconsumed_tail
        while unconsumed:
            max_chunk = (max_length + 1 - len(buf)) if max_length else 0
            decompressed = decompressor.decompress(unconsumed, max_chunk)
            buf.extend(decompressed)
            if max_length and len(buf) > max_length:
                raise RpcError(
                    StatusCode.RESOURCE_EXHAUSTED,
                    "Decompressed message larger than max (%d vs. %d)" % (len(buf), max_length),
                )
            if not decompressed:
                break
            unconsumed = decompressor.unconsumed_tail
        buf.extend(decompressor.flush())
        if max_length and len(buf) > max_length:
            raise RpcError(
                StatusCode.RESOURCE_EXHAUSTED,
                "Decompressed message larger than max (%d vs. %d)" % (len(buf), max_length),
            )
        return bytes(buf)
    except zlib.error as exc:
        raise RpcError(StatusCode.INTERNAL, "Corrupted gzip payload: %s" % exc)


def _unframe_message(data, encoding=None, max_length=None):
    if len(data) < 5:
        raise RpcError(StatusCode.INTERNAL, "Incomplete gRPC frame (%d bytes)" % len(data))
    compressed, length = struct.unpack(">BI", data[:5])
    if max_length and (0 < max_length < length):
        raise RpcError(
            StatusCode.RESOURCE_EXHAUSTED,
            "Received message larger than max (%d vs. %d)" % (length, max_length),
        )
    if len(data) != 5 + length:
        raise RpcError(
            StatusCode.INTERNAL,
            "Truncated or corrupted gRPC message (expected %d bytes, got %d)" % (length, len(data) - 5),
        )
    payload = data[5:5 + length]
    if compressed == 1:
        if encoding == "gzip":
            payload = _decompress_gzip_bounded(payload, max_length=max_length)
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


class _StreamRefusedBeforeResponse(Exception):
    """Internal error when a pooled connection stream is refused before processing."""


class _StaleConnectionError(Exception):
    """Internal error when a reused connection is reset/dropped before any response."""


class _Http2ClientConnection(object):
    """Single synchronous HTTP/2 client connection managed via h2."""

    def __init__(self, host, port, deadline=None):
        self.host = host
        self.port = port
        self.closed = False
        self._protocol_closed = False
        remaining = self._remaining_timeout(deadline) if deadline is not None else None
        self._sock = socket.create_connection((host, port), timeout=remaining)
        try:
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        config = h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
        self._conn = h2.connection.H2Connection(config=config)
        self._conn.initiate_connection()
        self._flush()

    def is_readable_or_closed(self):
        """Checks if connection has incoming data or EOF without blocking."""
        if self.closed:
            return True
        try:
            self._sock.setblocking(False)
            buf = self._sock.recv(1, socket.MSG_PEEK)
            self._sock.setblocking(True)
            if buf == b"":
                self.closed = True
                return True
            return False
        except (BlockingIOError, socket.error):
            try:
                self._sock.setblocking(True)
            except OSError:
                pass
            return False

    def _flush(self):
        out = self._conn.data_to_send()
        if out:
            self._sock.sendall(out)

    def _remaining_timeout(self, deadline):
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self.closed = True
            raise RpcError(StatusCode.DEADLINE_EXCEEDED, "Deadline exceeded")
        return remaining

    def _read_events(self, deadline):
        """Reads incoming socket data and returns decoded H2 events."""
        self._sock.settimeout(self._remaining_timeout(deadline))
        try:
            raw = self._sock.recv(65536)
        except socket.timeout:
            self.closed = True
            raise RpcError(StatusCode.DEADLINE_EXCEEDED, "Deadline exceeded")
        if not raw:
            self.closed = True
            raise EOFError("Connection closed by peer")
        events = self._conn.receive_data(raw)
        self._flush()
        return events

    def call(self, method_path, payload, deadline=None, metadata=None,
             max_send_length=None, max_recv_length=None, reused=False):
        """Executes an RPC call over HTTP/2."""
        if max_send_length and (0 < max_send_length < len(payload)):
            raise RpcError(
                StatusCode.RESOURCE_EXHAUSTED,
                "Sent message larger than max (%d vs. %d)" % (len(payload), max_send_length),
            )

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
        remaining = self._remaining_timeout(deadline) if deadline is not None else None
        timeout_hdr = _format_grpc_timeout(remaining)
        if timeout_hdr:
            headers.append(("grpc-timeout", timeout_hdr))

        if metadata:
            for key, val in metadata:
                key_str = str(key).lower()
                if not key_str.startswith(":"):
                    headers.append((key_str, str(val)))

        initial_headers = {}
        trailing_headers = {}
        received_any = False
        data_chunks = []
        cumulative_recv_len = 0
        stream_ended = False
        stream_reset = None

        def _process_events(events):
            nonlocal stream_ended, stream_reset, received_any, cumulative_recv_len
            for event in events:
                if isinstance(event, h2.events.ResponseReceived) and event.stream_id == stream_id:
                    received_any = True
                    initial_headers.update(_decode_headers(event.headers))
                elif isinstance(event, h2.events.DataReceived) and event.stream_id == stream_id:
                    received_any = True
                    if event.data:
                        if max_recv_length and (cumulative_recv_len + len(event.data)) > (max_recv_length + 5):
                            try:
                                self._conn.reset_stream(stream_id, error_code=h2.errors.ErrorCodes.CANCEL)
                                self._flush()
                            except (OSError, h2.exceptions.H2Error):
                                pass
                            raise RpcError(
                                StatusCode.RESOURCE_EXHAUSTED,
                                "Received message exceeds limit of %d bytes" % max_recv_length,
                            )
                        cumulative_recv_len += len(event.data)
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
                try:
                    self._flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
                offset += chunk_size

            if total == 0 and not stream_ended:
                self._conn.end_stream(stream_id)
                try:
                    self._flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            while not stream_ended:
                _process_events(self._read_events(deadline))
        except RpcError:
            if not stream_ended:
                self.closed = True
            raise
        except (EOFError, BrokenPipeError, ConnectionResetError, h2.exceptions.H2Error) as exc:
            self.closed = True
            if (trailing_headers or "grpc-status" in initial_headers) and not stream_ended:
                stream_ended = True
            elif reused and not received_any:
                raise _StaleConnectionError(str(exc))
            else:
                raise RpcError(StatusCode.UNAVAILABLE, str(exc))
        except socket.timeout:
            self.closed = True
            raise RpcError(StatusCode.DEADLINE_EXCEEDED, "Deadline exceeded")
        except OSError as exc:
            self.closed = True
            if reused and not received_any:
                raise _StaleConnectionError(str(exc))
            raise RpcError(StatusCode.UNAVAILABLE, str(exc))

        if (stream_reset is not None and not trailing_headers
                and "grpc-status" not in initial_headers):
            if stream_reset == h2.errors.ErrorCodes.CANCEL:
                raise RpcError(StatusCode.CANCELLED, "Stream cancelled")
            if stream_reset == h2.errors.ErrorCodes.REFUSED_STREAM:
                if reused and not received_any:
                    raise _StreamRefusedBeforeResponse("Stream refused")
                raise RpcError(StatusCode.UNAVAILABLE, "Stream refused")
            raise RpcError(StatusCode.INTERNAL, "HTTP/2 stream reset: %s" % stream_reset)

        raw_status = trailing_headers.get("grpc-status", initial_headers.get("grpc-status"))
        raw_message = trailing_headers.get(
            "grpc-message", initial_headers.get("grpc-message", "")
        )
        grpc_message = unquote(raw_message) if raw_message else ""

        content_type = initial_headers.get("content-type", "")
        is_grpc_content = content_type.startswith("application/grpc")

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
        if status_code == StatusCode.OK:
            http_status_str = initial_headers.get(":status")
            if http_status_str and http_status_str != "200":
                try:
                    http_status = int(http_status_str)
                except ValueError:
                    http_status = 0
                raise RpcError(_http_status_to_grpc(http_status), "HTTP status %s" % http_status_str)
            if not is_grpc_content and "content-type" in initial_headers:
                raise RpcError(StatusCode.INTERNAL, "Invalid content-type: %s" % content_type)
        else:
            raise RpcError(status_code, grpc_message)

        encoding = initial_headers.get("grpc-encoding")
        return _unframe_message(
            b"".join(data_chunks), encoding=encoding, max_length=max_recv_length
        )

    def close(self):
        """Closes the underlying socket and terminates the HTTP/2 connection."""
        if not self._protocol_closed:
            self._protocol_closed = True
            try:
                self._conn.close_connection()
                out = self._conn.data_to_send()
                if out:
                    self._sock.settimeout(0.5)
                    self._sock.sendall(out)
            except (OSError, h2.exceptions.H2Error):
                pass
        self.closed = True
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
        raw_response = self._channel._invoke(self._method, payload, timeout=timeout, metadata=metadata)
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
        self._active = set()
        self._lock = threading.Lock()
        self._closed = False

    def _acquire(self, deadline=None):
        with self._lock:
            if self._closed:
                raise RpcError(StatusCode.UNAVAILABLE, "Channel is closed")
            while self._pool:
                conn = self._pool.pop()
                if not conn.closed and not conn.is_readable_or_closed():
                    self._active.add(conn)
                    return conn, True
                conn.close()
        try:
            conn = _Http2ClientConnection(self._host, self._port, deadline=deadline)
        except socket.timeout:
            raise RpcError(StatusCode.DEADLINE_EXCEEDED, "Connection timed out")
        except OSError as exc:
            raise RpcError(StatusCode.UNAVAILABLE, "Failed to connect to %s:%d: %s" % (
                self._host, self._port, exc
            ))

        with self._lock:
            if self._closed:
                conn.close()
                raise RpcError(StatusCode.UNAVAILABLE, "Channel is closed")
            self._active.add(conn)
            return conn, False

    def _release(self, conn):
        with self._lock:
            self._active.discard(conn)
            if conn.closed or self._closed or len(self._pool) >= self._max_pool_size:
                conn.close()
            else:
                self._pool.append(conn)

    def _invoke(self, method_path, payload, timeout=None, metadata=None):
        deadline = (time.monotonic() + float(timeout)) if timeout is not None else None
        conn, reused = self._acquire(deadline=deadline)
        try:
            try:
                return conn.call(
                    method_path,
                    payload,
                    deadline=deadline,
                    metadata=metadata,
                    max_send_length=self._max_send_length,
                    max_recv_length=self._max_recv_length,
                    reused=reused,
                )
            except (_StreamRefusedBeforeResponse, _StaleConnectionError):
                conn.close()
                if deadline is not None and (deadline - time.monotonic()) <= 0:
                    raise RpcError(StatusCode.DEADLINE_EXCEEDED, "Deadline exceeded")
                conn, _ = self._acquire(deadline=deadline)
                return conn.call(
                    method_path,
                    payload,
                    deadline=deadline,
                    metadata=metadata,
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
        """Closes all pooled and active connections and marks channel as closed."""
        with self._lock:
            self._closed = True
            to_close = list(self._pool) + list(self._active)
            self._pool.clear()
            self._active.clear()
        for conn in to_close:
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
        self._channel = channel
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

    def __getattr__(self, name):
        raise AttributeError("'%s' object has no attribute '%s'" % (
            self.__class__.__name__, name
        ))


def stub_factory(pb2_module, service_name):
    """Returns a stub constructor taking (channel) for the given protobuf service."""
    service_descriptor = pb2_module.DESCRIPTOR.services_by_name[service_name]

    def _create_stub(channel):
        return DynamicStub(channel, service_descriptor)

    _create_stub.__name__ = "%sStub" % service_name
    return _create_stub


class RpcContext(object):
    """Server-side RPC context passed to servicer methods."""

    def __init__(self):
        self._code = StatusCode.OK
        self._details = ""

    def set_code(self, code):
        """Sets the RPC status code."""
        self._code = code

    def set_details(self, details):
        """Sets the RPC status detail message."""
        self._details = details

    def code(self):
        """Returns the current RPC status code."""
        return self._code

    def details(self):
        """Returns the current RPC status detail message."""
        return self._details


def _encode_grpc_message(message):
    if not message:
        return ""
    safe_chars = (
        " !#$&'()*+,-./0123456789:;<=>?@"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~"
    )
    return quote(str(message), safe=safe_chars)


class _ServerConnectionHandler(object):
    """Manages a single incoming HTTP/2 connection on Http2Server."""

    def __init__(self, conn_sock, server_instance):
        self._sock = conn_sock
        self._server = server_instance
        config = h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
        self._conn = h2.connection.H2Connection(config=config)
        self._write_lock = threading.Lock()
        self._window_cond = threading.Condition(self._write_lock)
        self._closed = False
        self._protocol_closed = False
        self._streams = {}
        self._reset_streams = set()

    def _flush_locked(self):
        out = self._conn.data_to_send()
        if out:
            self._sock.sendall(out)

    def _send_trailers_only(self, stream_id, status_code, message=""):
        headers = [
            (":status", "200"),
            ("content-type", "application/grpc"),
            ("grpc-status", str(int(status_code))),
        ]
        if message:
            headers.append(("grpc-message", _encode_grpc_message(message)))
        with self._write_lock:
            if self._closed or stream_id in self._reset_streams:
                return
            try:
                self._conn.send_headers(stream_id, headers, end_stream=True)
                self._flush_locked()
            except h2.exceptions.StreamClosedError:
                self._reset_streams.add(stream_id)
            except (OSError, h2.exceptions.H2Error):
                self._closed = True

    def _send_response(self, stream_id, payload, status_code=StatusCode.OK, message="", deadline=None):
        framed = _frame_message(payload)
        init_headers = [
            (":status", "200"),
            ("content-type", "application/grpc"),
        ]
        trailers = [
            ("grpc-status", str(int(status_code))),
        ]
        if message:
            trailers.append(("grpc-message", _encode_grpc_message(message)))

        with self._write_lock:
            if self._closed or stream_id in self._reset_streams:
                return
            try:
                self._conn.send_headers(stream_id, init_headers)
                self._flush_locked()

                offset = 0
                total = len(framed)
                while offset < total:
                    window = self._conn.local_flow_control_window(stream_id)
                    while window <= 0 and not self._closed and stream_id not in self._reset_streams:
                        now = time.monotonic()
                        if deadline is not None and now >= deadline:
                            try:
                                self._conn.reset_stream(stream_id, error_code=h2.errors.ErrorCodes.CANCEL)
                                self._flush_locked()
                            except (OSError, h2.exceptions.H2Error):
                                pass
                            self._reset_streams.add(stream_id)
                            return
                        wait_timeout = min(0.5, max(0.01, deadline - now)) if deadline is not None else 0.5
                        self._window_cond.wait(timeout=wait_timeout)
                        if self._closed or stream_id in self._reset_streams:
                            return
                        window = self._conn.local_flow_control_window(stream_id)

                    chunk_size = min(window, self._conn.max_outbound_frame_size, total - offset)
                    self._conn.send_data(stream_id, framed[offset:offset + chunk_size])
                    self._flush_locked()
                    offset += chunk_size

                self._conn.send_headers(stream_id, trailers, end_stream=True)
                self._flush_locked()
            except h2.exceptions.StreamClosedError:
                self._reset_streams.add(stream_id)
            except (OSError, h2.exceptions.H2Error):
                self._closed = True

    def _dispatch_rpc(self, stream_id, headers, body, deadline=None):
        try:
            if self._server.is_stopped:
                self._send_trailers_only(
                    stream_id, StatusCode.UNAVAILABLE, "Server is stopping"
                )
                return

            if deadline is not None and time.monotonic() >= deadline:
                self._send_trailers_only(
                    stream_id, StatusCode.DEADLINE_EXCEEDED, "Deadline exceeded before execution"
                )
                return

            method = headers.get(":method", "")
            if method != "POST":
                self._send_trailers_only(
                    stream_id, StatusCode.INTERNAL, "Method not allowed: %s" % method
                )
                return

            content_type = headers.get("content-type", "")
            if not content_type.startswith("application/grpc"):
                self._send_trailers_only(
                    stream_id, StatusCode.UNKNOWN, "Unsupported media type: %s" % content_type
                )
                return

            path = headers.get(":path", "")
            entry = self._server.lookup_method(path)
            if entry is None:
                self._send_trailers_only(
                    stream_id, StatusCode.UNIMPLEMENTED, "Method not found: %s" % path
                )
                return

            req_cls, handler = entry
            try:
                encoding = headers.get("grpc-encoding")
                req_bytes = _unframe_message(
                    body, encoding=encoding, max_length=self._server._max_recv_length
                )
                request = req_cls.FromString(req_bytes)
            except RpcError as exc:
                self._send_trailers_only(stream_id, exc.code(), exc.details())
                return
            except Exception as exc:  # pylint: disable=broad-except
                self._send_trailers_only(stream_id, StatusCode.INVALID_ARGUMENT, str(exc))
                return

            ctx = RpcContext()
            try:
                response = handler(request, ctx)
            except Exception as exc:  # pylint: disable=broad-except
                log.exception("Unhandled exception in RPC handler for %s", path)
                self._send_trailers_only(stream_id, StatusCode.INTERNAL, str(exc))
                return

            if ctx.code() != StatusCode.OK:
                self._send_trailers_only(stream_id, ctx.code(), ctx.details())
                return

            try:
                resp_bytes = response.SerializeToString() if response is not None else b""
            except Exception as exc:  # pylint: disable=broad-except
                self._send_trailers_only(stream_id, StatusCode.INTERNAL, "Serialization error: %s" % exc)
                return

            self._send_response(stream_id, resp_bytes, StatusCode.OK, deadline=deadline)
        finally:
            self._server.active_calls_decrement()

    def serve(self):
        """Serves the connection until closed or server stops."""
        try:
            self._sock.settimeout(0.5)
            try:
                self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

            with self._write_lock:
                self._conn.initiate_connection()
                self._flush_locked()

            while not self._server.is_stopped and not self._closed:
                try:
                    raw = self._sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not raw:
                    break

                pending_dispatches = []
                with self._write_lock:
                    try:
                        events = self._conn.receive_data(raw)
                    except h2.exceptions.H2Error:
                        break

                    for event in events:
                        if isinstance(event, h2.events.RequestReceived):
                            hdrs = _decode_headers(event.headers)
                            timeout_val = _parse_grpc_timeout(hdrs.get("grpc-timeout"))
                            deadline = (time.monotonic() + timeout_val) if timeout_val is not None else None
                            self._streams[event.stream_id] = {
                                "headers": hdrs,
                                "chunks": [],
                                "bytes_received": 0,
                                "rejected": False,
                                "deadline": deadline,
                            }
                        elif isinstance(event, h2.events.DataReceived):
                            stream = self._streams.get(event.stream_id)
                            if stream is not None and not stream.get("rejected"):
                                if event.data:
                                    max_len = self._server._max_recv_length
                                    if max_len and (stream["bytes_received"] + len(event.data)) > (max_len + 5):
                                        stream["rejected"] = True
                                        stream["chunks"].clear()
                                        try:
                                            self._conn.reset_stream(event.stream_id, error_code=h2.errors.ErrorCodes.CANCEL)
                                        except (OSError, h2.exceptions.H2Error):
                                            pass
                                        self._send_trailers_only(
                                            event.stream_id,
                                            StatusCode.RESOURCE_EXHAUSTED,
                                            "Received message larger than max (%d bytes)" % max_len,
                                        )
                                    else:
                                        stream["bytes_received"] += len(event.data)
                                        stream["chunks"].append(event.data)
                            if event.flow_controlled_length:
                                self._conn.acknowledge_received_data(
                                    event.flow_controlled_length, event.stream_id
                                )
                        elif isinstance(event, h2.events.WindowUpdated):
                            self._window_cond.notify_all()
                        elif isinstance(event, h2.events.StreamEnded):
                            stream = self._streams.pop(event.stream_id, None)
                            if stream is not None and not stream.get("rejected"):
                                pending_dispatches.append(
                                    (event.stream_id, stream["headers"], b"".join(stream["chunks"]), stream["deadline"])
                                )
                        elif isinstance(event, h2.events.StreamReset):
                            self._streams.pop(event.stream_id, None)
                            self._reset_streams.add(event.stream_id)
                            self._window_cond.notify_all()
                        elif isinstance(event, h2.events.ConnectionTerminated):
                            self._closed = True
                            self._window_cond.notify_all()

                    try:
                        self._flush_locked()
                    except OSError:
                        break

                for stream_id, hdrs, body, deadline in pending_dispatches:
                    self._server.active_calls_increment()
                    self._server.submit_task(self._dispatch_rpc, stream_id, hdrs, body, deadline)
        finally:
            self.close()

    def close(self):
        """Closes the client connection and releases socket resources."""
        with self._write_lock:
            if not self._protocol_closed:
                self._protocol_closed = True
                self._window_cond.notify_all()
                try:
                    self._conn.close_connection()
                    out = self._conn.data_to_send()
                    if out:
                        self._sock.settimeout(0.2)
                        self._sock.sendall(out)
                except OSError:
                    pass
            self._closed = True
            try:
                self._sock.close()
            except OSError:
                pass


class Http2Server(object):
    """Pure-Python HTTP/2 gRPC server backed by h2 and a ThreadPoolExecutor."""

    DEFAULT_MAX_RECEIVE_MESSAGE_LENGTH = 4 * 1024 * 1024

    def __init__(self, thread_pool, options=None):
        self._thread_pool = thread_pool
        self._max_recv_length = self.DEFAULT_MAX_RECEIVE_MESSAGE_LENGTH
        if options:
            for key, value in options:
                if key == "grpc.max_receive_message_length":
                    self._max_recv_length = int(value)
        self._methods = {}
        self._methods_lock = threading.Lock()
        self._listen_socks = []
        self._bound_ports = []
        self._stop_event = threading.Event()
        self._accept_threads = []
        self._conn_handlers = set()
        self._conn_lock = threading.Lock()
        self._active_calls = 0
        self._calls_lock = threading.Lock()
        self._calls_zero_cond = threading.Condition(self._calls_lock)

    @property
    def is_stopped(self):
        """Returns whether the server has been requested to stop."""
        return self._stop_event.is_set()

    def active_calls_increment(self):
        """Increments active running RPC dispatch count."""
        with self._calls_lock:
            self._active_calls += 1

    def active_calls_decrement(self):
        """Decrements active running RPC dispatch count."""
        with self._calls_lock:
            self._active_calls -= 1
            if self._active_calls <= 0:
                self._calls_zero_cond.notify_all()

    def add_insecure_port(self, address):
        """Binds the server to an insecure TCP port."""
        host, port = _parse_target(address, default_port=0)
        sock = None
        if host in ("[::]", "::", ""):
            try:
                sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except OSError:
                    pass
                sock.bind(("::", port))
            except OSError:
                if sock is not None:
                    sock.close()
                sock = None
        if sock is None:
            bind_host = "0.0.0.0" if host in ("[::]", "::", "") else host
            family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((bind_host, port))
            except OSError:
                sock.close()
                raise

        try:
            sock.listen(128)
            sock.settimeout(0.5)
        except OSError:
            sock.close()
            raise

        actual_port = sock.getsockname()[1]
        self._listen_socks.append(sock)
        self._bound_ports.append(actual_port)
        return actual_port

    def bound_port(self):
        """Returns the primary bound listening port."""
        return self._bound_ports[0] if self._bound_ports else None

    def add_service(self, pb2_module, service_name, servicer):
        """Registers a protobuf service and its servicer implementation."""
        service_descriptor = pb2_module.DESCRIPTOR.services_by_name[service_name]
        sym_db = symbol_database.Default()
        with self._methods_lock:
            for method in service_descriptor.methods:
                handler = getattr(servicer, method.name, None)
                if handler is not None:
                    req_cls = sym_db.GetSymbol(method.input_type.full_name)
                    path = "/%s/%s" % (service_descriptor.full_name, method.name)
                    self._methods[path] = (req_cls, handler)

    def lookup_method(self, path):
        """Looks up a registered method by path."""
        with self._methods_lock:
            return self._methods.get(path)

    def submit_task(self, fn, *args):
        """Submits a dispatch task to the server thread pool."""
        try:
            self._thread_pool.submit(fn, *args)
        except RuntimeError:
            pass

    def _accept_loop(self, listen_sock):
        while not self._stop_event.is_set():
            try:
                conn_sock, _ = listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            handler = _ServerConnectionHandler(conn_sock, self)
            with self._conn_lock:
                self._conn_handlers.add(handler)
            thread = threading.Thread(target=self._run_connection, args=(handler,), daemon=True)
            thread.start()

    def _run_connection(self, handler):
        try:
            handler.serve()
        finally:
            with self._conn_lock:
                self._conn_handlers.discard(handler)

    def start(self):
        """Starts listener accept threads."""
        self._stop_event.clear()
        for listen_sock in self._listen_socks:
            thread = threading.Thread(
                target=self._accept_loop, args=(listen_sock,), daemon=True
            )
            self._accept_threads.append(thread)
            thread.start()

    def stop(self, grace=None):
        """Stops listeners and closes active connections."""
        self._stop_event.set()
        for sock in self._listen_socks:
            try:
                sock.close()
            except OSError:
                pass

        if grace and grace > 0:
            grace_deadline = time.monotonic() + grace
            with self._calls_lock:
                while self._active_calls > 0:
                    remaining = grace_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._calls_zero_cond.wait(timeout=min(0.2, remaining))

        with self._conn_lock:
            handlers = list(self._conn_handlers)
        for handler in handlers:
            handler.close()
        timeout = min(grace, 2.0) if grace is not None else 0.5
        for thread in self._accept_threads:
            thread.join(timeout=timeout)


def server(thread_pool, options=None, *_args, **_kwargs):
    """Creates and returns an Http2Server instance."""
    return Http2Server(thread_pool, options=options)
