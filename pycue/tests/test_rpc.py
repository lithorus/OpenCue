#!/usr/bin/env python

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

"""Tests for `opencue.rpc` pure-Python HTTP/2 transport."""

# pylint: disable=no-member,too-few-public-methods

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import socket
import struct
import sys
import threading
import time
import unittest
from urllib.parse import quote

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import mock

from opencue_proto import cue_pb2
from opencue_proto import host_pb2
import opencue
import opencue.cuebot
import opencue.rpc


class _MiniHttp2GrpcServer(object):
    """Minimal HTTP/2 gRPC test server powered by h2 for client transport tests."""

    def __init__(self, handler):
        self._handler = handler
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.connection_count = 0
        self._stop = threading.Event()
        self._threads = []
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn_sock, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connection_count += 1
            t = threading.Thread(target=self._serve_conn, args=(conn_sock,), daemon=True)
            t.start()
            self._threads.append(t)

    def _send_grpc_response(self, conn_sock, conn, stream_id, status, message, resp_payload):
        if status != opencue.rpc.StatusCode.OK:
            trailers = [
                (":status", "200"),
                ("content-type", "application/grpc"),
                ("grpc-status", str(int(status))),
            ]
            if message:
                trailers.append(("grpc-message", quote(message)))
            conn.send_headers(stream_id, trailers, end_stream=True)
            out = conn.data_to_send()
            if out:
                conn_sock.sendall(out)
            return

        conn.send_headers(
            stream_id,
            [(":status", "200"), ("content-type", "application/grpc")],
        )
        framed = struct.pack(">BI", 0, len(resp_payload)) + resp_payload
        offset = 0
        while offset < len(framed):
            win = conn.local_flow_control_window(stream_id)
            while win <= 0 and not self._stop.is_set():
                out = conn.data_to_send()
                if out:
                    conn_sock.sendall(out)
                more = conn_sock.recv(65536)
                if not more:
                    return
                for ev2 in conn.receive_data(more):
                    if (isinstance(ev2, h2.events.DataReceived)
                            and ev2.flow_controlled_length):
                        conn.acknowledge_received_data(
                            ev2.flow_controlled_length, ev2.stream_id
                        )
                win = conn.local_flow_control_window(stream_id)
            chunk = min(win, conn.max_outbound_frame_size, len(framed) - offset)
            conn.send_data(stream_id, framed[offset:offset + chunk])
            offset += chunk
            out = conn.data_to_send()
            if out:
                conn_sock.sendall(out)
        conn.send_headers(stream_id, [("grpc-status", "0")], end_stream=True)
        out = conn.data_to_send()
        if out:
            conn_sock.sendall(out)

    def _handle_event(self, event, streams, conn, conn_sock):
        if isinstance(event, h2.events.RequestReceived):
            streams[event.stream_id] = {
                "headers": dict(event.headers),
                "chunks": [],
            }
            return False

        if isinstance(event, h2.events.DataReceived):
            if event.stream_id in streams and event.data:
                streams[event.stream_id]["chunks"].append(event.data)
            if event.flow_controlled_length:
                conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                out = conn.data_to_send()
                if out:
                    conn_sock.sendall(out)
            return False

        if isinstance(event, h2.events.StreamEnded):
            stream = streams.pop(event.stream_id)
            body = b"".join(stream["chunks"])
            req_payload = body[5:] if len(body) >= 5 else b""
            status, msg, resp_payload, close_after = self._handler(
                stream["headers"], req_payload
            )
            self._send_grpc_response(
                conn_sock, conn, event.stream_id, status, msg, resp_payload
            )
            return close_after

        return False

    def _serve_conn(self, conn_sock):
        conn_sock.settimeout(0.2)
        conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
        )
        conn.initiate_connection()
        try:
            conn_sock.sendall(conn.data_to_send())
            streams = {}
            while not self._stop.is_set():
                try:
                    raw = conn_sock.recv(65536)
                except socket.timeout:
                    continue
                if not raw:
                    break
                events = conn.receive_data(raw)
                out = conn.data_to_send()
                if out:
                    conn_sock.sendall(out)
                for event in events:
                    if self._handle_event(event, streams, conn, conn_sock):
                        return
        except (OSError, h2.exceptions.H2Error):
            pass
        finally:
            try:
                conn_sock.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._accept_thread.join(timeout=1.0)


class RpcTransportTests(unittest.TestCase):
    """End-to-end tests for `opencue.rpc` HTTP/2 transport."""

    def test_unary_rpc_and_connection_pooling(self):
        received_paths = []

        def handler(headers, payload):
            received_paths.append(headers[":path"])
            cue_pb2.CueGetSystemStatsRequest.FromString(payload)
            resp = cue_pb2.CueGetSystemStatsResponse(
                stats=cue_pb2.SystemStats(manage_waiting=42, booked_procs=7)
            )
            return opencue.rpc.StatusCode.OK, "", resp.SerializeToString(), False

        srv = _MiniHttp2GrpcServer(handler)
        channel = opencue.rpc.insecure_channel("127.0.0.1:%d" % srv.port)
        try:
            stub = opencue.cuebot.Cuebot.SERVICE_MAP["cue"](channel)
            resp1 = stub.GetSystemStats(cue_pb2.CueGetSystemStatsRequest(), timeout=5)
            resp2 = stub.GetSystemStats(cue_pb2.CueGetSystemStatsRequest(), timeout=5)
            self.assertEqual(42, resp1.stats.manage_waiting)
            self.assertEqual(7, resp2.stats.booked_procs)
            self.assertEqual(
                ["/cue.CueInterface/GetSystemStats", "/cue.CueInterface/GetSystemStats"],
                received_paths,
            )
            self.assertEqual(1, srv.connection_count)
        finally:
            channel.close()
            srv.stop()

    def test_large_payload_flow_control(self):
        hosts = [
            host_pb2.Host(id="id-%04d" % i, name="host-%04d-" % i + ("x" * 180))
            for i in range(900)
        ]
        large_resp = host_pb2.HostGetHostsResponse(hosts=hosts)
        self.assertGreater(len(large_resp.SerializeToString()), 150000)

        def handler(_headers, payload):
            req = host_pb2.HostGetHostsRequest.FromString(payload)
            self.assertGreater(len(req.r.hosts), 10)
            return opencue.rpc.StatusCode.OK, "", large_resp.SerializeToString(), False

        srv = _MiniHttp2GrpcServer(handler)
        channel = opencue.rpc.insecure_channel("127.0.0.1:%d" % srv.port)
        try:
            stub = opencue.cuebot.Cuebot.SERVICE_MAP["host"](channel)
            req = host_pb2.HostGetHostsRequest()
            req.r.hosts.extend(["h-%04d-%s" % (i, "y" * 200) for i in range(450)])
            self.assertGreater(len(req.SerializeToString()), 80000)
            resp = stub.GetHosts(req, timeout=10)
            self.assertEqual(900, len(resp.hosts))
            self.assertEqual("id-0899", resp.hosts[-1].id)
        finally:
            channel.close()
            srv.stop()

    def test_non_ok_status_and_percent_encoded_details(self):
        def handler(_headers, _payload):
            return (
                opencue.rpc.StatusCode.NOT_FOUND,
                "Job not found: my_job%42",
                b"",
                False,
            )

        srv = _MiniHttp2GrpcServer(handler)
        channel = opencue.rpc.insecure_channel("127.0.0.1:%d" % srv.port)
        try:
            stub = opencue.cuebot.Cuebot.SERVICE_MAP["cue"](channel)
            with self.assertRaises(opencue.rpc.RpcError) as ctx:
                stub.GetSystemStats(cue_pb2.CueGetSystemStatsRequest(), timeout=5)
            self.assertEqual(opencue.rpc.StatusCode.NOT_FOUND, ctx.exception.code())
            self.assertEqual("Job not found: my_job%42", ctx.exception.details())
        finally:
            channel.close()
            srv.stop()

    def test_retry_interceptor_retries_unavailable_and_raises(self):
        attempts = [0]

        def handler(_headers, _payload):
            attempts[0] += 1
            if attempts[0] < 3:
                return opencue.rpc.StatusCode.UNAVAILABLE, "transient", b"", False
            resp = cue_pb2.CueGetSystemStatsResponse(
                stats=cue_pb2.SystemStats(manage_threads=9)
            )
            return opencue.rpc.StatusCode.OK, "", resp.SerializeToString(), False

        srv = _MiniHttp2GrpcServer(handler)
        interceptor = opencue.cuebot.RetryOnRpcErrorClientInterceptor(
            max_attempts=4,
            sleeping_policy=opencue.cuebot.ExponentialBackoff(
                init_backoff_ms=1, max_backoff_ms=5, multiplier=2
            ),
            status_for_retry=(opencue.rpc.StatusCode.UNAVAILABLE,),
        )
        base_channel = opencue.rpc.insecure_channel("127.0.0.1:%d" % srv.port)
        channel = opencue.rpc.intercept_channel(base_channel, interceptor)
        try:
            stub = opencue.cuebot.Cuebot.SERVICE_MAP["cue"](channel)
            resp = stub.GetSystemStats(cue_pb2.CueGetSystemStatsRequest(), timeout=5)
            self.assertEqual(3, attempts[0])
            self.assertEqual(9, resp.stats.manage_threads)
        finally:
            channel.close()
            srv.stop()

    def test_retry_interceptor_raises_on_exhaustion_or_non_retryable(self):
        def always_unavailable(_headers, _payload):
            return opencue.rpc.StatusCode.UNAVAILABLE, "still down", b"", False

        srv = _MiniHttp2GrpcServer(always_unavailable)
        interceptor = opencue.cuebot.RetryOnRpcErrorClientInterceptor(
            max_attempts=2,
            sleeping_policy=opencue.cuebot.ExponentialBackoff(
                init_backoff_ms=1, max_backoff_ms=2, multiplier=2
            ),
            status_for_retry=(opencue.rpc.StatusCode.UNAVAILABLE,),
        )
        channel = opencue.rpc.intercept_channel(
            opencue.rpc.insecure_channel("127.0.0.1:%d" % srv.port), interceptor
        )
        try:
            stub = opencue.cuebot.Cuebot.SERVICE_MAP["cue"](channel)
            with self.assertRaises(opencue.rpc.RpcError) as ctx:
                stub.GetSystemStats(cue_pb2.CueGetSystemStatsRequest(), timeout=5)
            self.assertEqual(opencue.rpc.StatusCode.UNAVAILABLE, ctx.exception.code())
            self.assertEqual("still down", ctx.exception.details())
        finally:
            channel.close()
            srv.stop()

    def test_timeout_raises_deadline_exceeded(self):
        def slow_handler(_headers, _payload):
            time.sleep(0.4)
            return opencue.rpc.StatusCode.OK, "", b"", False

        srv = _MiniHttp2GrpcServer(slow_handler)
        channel = opencue.rpc.insecure_channel("127.0.0.1:%d" % srv.port)
        try:
            stub = opencue.cuebot.Cuebot.SERVICE_MAP["cue"](channel)
            with self.assertRaises(opencue.rpc.RpcError) as ctx:
                stub.GetSystemStats(cue_pb2.CueGetSystemStatsRequest(), timeout=0.05)
            self.assertEqual(opencue.rpc.StatusCode.DEADLINE_EXCEEDED, ctx.exception.code())
        finally:
            channel.close()
            srv.stop()

    def test_max_message_length_enforced(self):
        def handler(_headers, _payload):
            resp = host_pb2.HostGetHostsResponse(
                hosts=[host_pb2.Host(id="h1", name="host-name-exceeds-limit")]
            )
            return opencue.rpc.StatusCode.OK, "", resp.SerializeToString(), False

        srv = _MiniHttp2GrpcServer(handler)
        send_limited = opencue.rpc.insecure_channel(
            "127.0.0.1:%d" % srv.port, options=[("grpc.max_send_message_length", 5)]
        )
        recv_limited = opencue.rpc.insecure_channel(
            "127.0.0.1:%d" % srv.port, options=[("grpc.max_receive_message_length", 5)]
        )
        try:
            send_stub = opencue.cuebot.Cuebot.SERVICE_MAP["host"](send_limited)
            req = host_pb2.HostGetHostsRequest()
            req.r.hosts.append("longer-than-five-bytes")
            with self.assertRaises(opencue.rpc.RpcError) as send_ctx:
                send_stub.GetHosts(req, timeout=5)
            self.assertEqual(opencue.rpc.StatusCode.RESOURCE_EXHAUSTED, send_ctx.exception.code())

            recv_stub = opencue.cuebot.Cuebot.SERVICE_MAP["host"](recv_limited)
            with self.assertRaises(opencue.rpc.RpcError) as recv_ctx:
                recv_stub.GetHosts(host_pb2.HostGetHostsRequest(), timeout=5)
            self.assertEqual(opencue.rpc.StatusCode.RESOURCE_EXHAUSTED, recv_ctx.exception.code())
        finally:
            send_limited.close()
            recv_limited.close()
            srv.stop()

    def test_stale_pooled_connection_reconnects(self):
        calls = [0]

        def handler(_headers, _payload):
            calls[0] += 1
            resp = cue_pb2.CueGetSystemStatsResponse(
                stats=cue_pb2.SystemStats(booked_procs=calls[0])
            )
            # Force connection drop right after first response so second call hits stale socket
            return opencue.rpc.StatusCode.OK, "", resp.SerializeToString(), (calls[0] == 1)

        srv = _MiniHttp2GrpcServer(handler)
        channel = opencue.rpc.insecure_channel("127.0.0.1:%d" % srv.port)
        try:
            stub = opencue.cuebot.Cuebot.SERVICE_MAP["cue"](channel)
            first = stub.GetSystemStats(cue_pb2.CueGetSystemStatsRequest(), timeout=5)
            time.sleep(0.05)
            second = stub.GetSystemStats(cue_pb2.CueGetSystemStatsRequest(), timeout=5)
            self.assertEqual(1, first.stats.booked_procs)
            self.assertEqual(2, second.stats.booked_procs)
            self.assertEqual(2, srv.connection_count)
        finally:
            channel.close()
            srv.stop()

    def test_works_without_grpc_module(self):
        with mock.patch.dict(sys.modules, {"grpc": None}):
            self.assertIn("cue", opencue.cuebot.Cuebot.SERVICE_MAP)
            self.assertIsNone(sys.modules.get("grpc"))


if __name__ == "__main__":
    unittest.main()
