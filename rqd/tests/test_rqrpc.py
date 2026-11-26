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

"""Tests for `rpc` pure-Python HTTP/2 client and server transport."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from concurrent import futures
import sys
import threading
import time
import unittest

import opencue_proto.report_pb2
import opencue_proto.rqd_pb2
from opencue_proto import rpc
import rqd.cuerqd
import rqd.rqdservicers
import rqd.rqnetwork


class _DummyServicer(object):
    """Test servicer implementing subset of RqdInterface methods."""

    def __init__(self, large_frame_count=0, delay=0.0):
        self.nimby_on_called = False
        self.large_frame_count = large_frame_count
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def ReportStatus(self, _request, _context):
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.calls += 1
        frames = [
            opencue_proto.report_pb2.RunningFrameInfo(
                frame_id="frame-%04d" % i,
                frame_name="render.%04d" % i,
                job_name="job-" + ("x" * 160),
            )
            for i in range(self.large_frame_count)
        ]
        report = opencue_proto.report_pb2.HostReport(
            host=opencue_proto.report_pb2.RenderHost(name="test-host", num_procs=4),
            frames=frames,
        )
        return opencue_proto.rqd_pb2.RqdStaticReportStatusResponse(host_report=report)

    def NimbyOn(self, _request, _context):
        self.nimby_on_called = True
        return opencue_proto.rqd_pb2.RqdStaticNimbyOnResponse()

    def GetRunFrame(self, request, context):
        if request.frame_id == "missing":
            context.set_code(rpc.StatusCode.NOT_FOUND)
            context.set_details("Frame not found: %s" % request.frame_id)
            return opencue_proto.rqd_pb2.RqdStaticGetRunFrameResponse()
        return opencue_proto.rqd_pb2.RqdStaticGetRunFrameResponse(
            run_frame=opencue_proto.rqd_pb2.RunFrame(
                frame_id=request.frame_id,
                job_name="demo-job",
            )
        )


class RqRpcTests(unittest.TestCase):
    """End-to-end tests for `rpc` Http2Server and Http2Channel."""

    def _start_server(self, servicer):
        srv = rpc.server(futures.ThreadPoolExecutor(max_workers=8))
        srv.add_service(opencue_proto.rqd_pb2, "RqdInterface", servicer)
        port = srv.add_insecure_port("127.0.0.1:0")
        srv.start()
        return srv, port

    def test_roundtrip_with_rqd_interface_stub(self):
        servicer = _DummyServicer()
        srv, port = self._start_server(servicer)
        channel = rpc.insecure_channel("127.0.0.1:%d" % port)
        try:
            stub = rqd.cuerqd.RqdInterfaceStub(channel)
            status_resp = stub.ReportStatus(
                opencue_proto.rqd_pb2.RqdStaticReportStatusRequest(), timeout=5
            )
            self.assertEqual("test-host", status_resp.host_report.host.name)
            self.assertEqual(4, status_resp.host_report.host.num_procs)

            stub.NimbyOn(opencue_proto.rqd_pb2.RqdStaticNimbyOnRequest(), timeout=5)
            self.assertTrue(servicer.nimby_on_called)

            rf_resp = stub.GetRunFrame(
                opencue_proto.rqd_pb2.RqdStaticGetRunFrameRequest(frame_id="f-123"), timeout=5
            )
            self.assertEqual("demo-job", rf_resp.run_frame.job_name)
        finally:
            channel.close()
            srv.stop(0)

    def test_servicer_error_status_and_unimplemented(self):
        servicer = _DummyServicer()
        srv, port = self._start_server(servicer)
        channel = rpc.insecure_channel("127.0.0.1:%d" % port)
        try:
            stub = rqd.cuerqd.RqdInterfaceStub(channel)
            with self.assertRaises(rpc.RpcError) as not_found:
                stub.GetRunFrame(
                    opencue_proto.rqd_pb2.RqdStaticGetRunFrameRequest(frame_id="missing"),
                    timeout=5,
                )
            self.assertEqual(rpc.StatusCode.NOT_FOUND, not_found.exception.code())
            self.assertEqual("Frame not found: missing", not_found.exception.details())

            with self.assertRaises(rpc.RpcError) as unimpl:
                stub.LockAll(opencue_proto.rqd_pb2.RqdStaticLockAllRequest(), timeout=5)
            self.assertEqual(rpc.StatusCode.UNIMPLEMENTED, unimpl.exception.code())
        finally:
            channel.close()
            srv.stop(0)

    def test_large_payload_flow_control(self):
        servicer = _DummyServicer(large_frame_count=900)
        srv, port = self._start_server(servicer)
        channel = rpc.insecure_channel("127.0.0.1:%d" % port)
        try:
            stub = rqd.cuerqd.RqdInterfaceStub(channel)
            resp = stub.ReportStatus(
                opencue_proto.rqd_pb2.RqdStaticReportStatusRequest(), timeout=10
            )
            self.assertGreater(len(resp.SerializeToString()), 150000)
            self.assertEqual(900, len(resp.host_report.frames))
            self.assertEqual("frame-0899", resp.host_report.frames[-1].frame_id)
        finally:
            channel.close()
            srv.stop(0)

    def test_concurrent_calls(self):
        servicer = _DummyServicer()
        srv, port = self._start_server(servicer)
        channel = rpc.insecure_channel("127.0.0.1:%d" % port)
        errors = []
        try:
            stub = rqd.cuerqd.RqdInterfaceStub(channel)

            def worker():
                try:
                    resp = stub.ReportStatus(
                        opencue_proto.rqd_pb2.RqdStaticReportStatusRequest(), timeout=5
                    )
                    self.assertEqual("test-host", resp.host_report.host.name)
                except Exception as exc:  # pylint: disable=broad-except
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            self.assertEqual([], errors)
            self.assertEqual(10, servicer.calls)
        finally:
            channel.close()
            srv.stop(0)

    def test_timeout_deadline_exceeded(self):
        servicer = _DummyServicer(delay=0.4)
        srv, port = self._start_server(servicer)
        channel = rpc.insecure_channel("127.0.0.1:%d" % port)
        try:
            stub = rqd.cuerqd.RqdInterfaceStub(channel)
            with self.assertRaises(rpc.RpcError) as ctx:
                stub.ReportStatus(
                    opencue_proto.rqd_pb2.RqdStaticReportStatusRequest(), timeout=0.05
                )
            self.assertEqual(rpc.StatusCode.DEADLINE_EXCEEDED, ctx.exception.code())
        finally:
            channel.close()
            srv.stop(0)

    def test_retry_interceptor_retries_and_raises(self):
        servicer = _DummyServicer()
        srv, port = self._start_server(servicer)
        interceptor = rqd.rqnetwork.RetryOnRpcErrorClientInterceptor(
            max_attempts=2,
            sleeping_policy=rqd.rqnetwork.ExponentialBackoff(
                init_backoff_ms=1, max_backoff_ms=2, multiplier=2
            ),
            status_for_retry=(rpc.StatusCode.UNAVAILABLE,),
        )
        channel = rpc.intercept_channel(
            rpc.insecure_channel("127.0.0.1:%d" % port), interceptor
        )
        try:
            stub = rqd.cuerqd.RqdInterfaceStub(channel)
            # Non-retryable NOT_FOUND should raise immediately without retrying
            with self.assertRaises(rpc.RpcError) as ctx:
                stub.GetRunFrame(
                    opencue_proto.rqd_pb2.RqdStaticGetRunFrameRequest(frame_id="missing"),
                    timeout=5,
                )
            self.assertEqual(rpc.StatusCode.NOT_FOUND, ctx.exception.code())

            # Retry interceptor succeeds after transient UNAVAILABLE
            attempts = [0]
            def flaky_continuation(details, req):
                attempts[0] += 1
                if attempts[0] == 1:
                    raise rpc.RpcError(rpc.StatusCode.UNAVAILABLE, "transient error")
                return "success"
            res = interceptor.intercept_unary_unary(
                flaky_continuation, rpc.ClientCallDetails(method="test"), None
            )
            self.assertEqual("success", res)
            self.assertEqual(2, attempts[0])
        finally:
            channel.close()
            srv.stop(0)

        # Closed port yields UNAVAILABLE after retries exhausted
        closed_channel = rpc.intercept_channel(
            rpc.insecure_channel("127.0.0.1:%d" % port), interceptor
        )
        try:
            closed_stub = rqd.cuerqd.RqdInterfaceStub(closed_channel)
            with self.assertRaises(rpc.RpcError) as unavail:
                closed_stub.ReportStatus(
                    opencue_proto.rqd_pb2.RqdStaticReportStatusRequest(), timeout=2
                )
            self.assertEqual(rpc.StatusCode.UNAVAILABLE, unavail.exception.code())
        finally:
            closed_channel.close()

    def test_rqd_modules_work_without_grpc(self):
        import subprocess
        code = (
            "import sys; sys.modules['grpc'] = None; "
            "import rqd.cuerqd; import rqd.rqdservicers; import rqd.rqnetwork; "
            "assert sys.modules.get('grpc') is None"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(0, res.returncode, res.stderr)


if __name__ == "__main__":
    unittest.main()
