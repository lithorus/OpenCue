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

"""Integration tests for RQD servicers and client stubs with `rpc`."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from concurrent import futures
import sys
import unittest

import mock

import opencue_proto.report_pb2
import opencue_proto.rqd_pb2
from opencue_proto import rpc
import rqd.cuerqd
import rqd.rqdservicers
import rqd.rqnetwork


class _DummyServicer(object):
    """Test servicer implementing subset of RqdInterface methods."""

    def __init__(self):
        self.nimby_on_called = False

    def ReportStatus(self, _request, _context):
        report = opencue_proto.report_pb2.HostReport(
            host=opencue_proto.report_pb2.RenderHost(name="test-host", num_procs=4),
            frames=[],
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


class RqRpcIntegrationTests(unittest.TestCase):
    """Integration tests for RQD stubs, servicers, and interceptors."""

    def _start_server(self, servicer):
        srv = rpc.server(futures.ThreadPoolExecutor(max_workers=4))
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

    def test_servicer_error_status(self):
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
        finally:
            channel.close()
            srv.stop(0)

    def test_retry_interceptor_retries_and_raises(self):
        interceptor = rqd.rqnetwork.RetryOnRpcErrorClientInterceptor(
            max_attempts=3,
            sleeping_policy=rqd.rqnetwork.ExponentialBackoff(
                init_backoff_ms=1, max_backoff_ms=4, multiplier=2
            ),
            status_for_retry=(rpc.StatusCode.UNAVAILABLE,),
        )

        attempts = [0]
        def flaky_continuation(_details, _req):
            attempts[0] += 1
            if attempts[0] < 3:
                raise rpc.RpcError(rpc.StatusCode.UNAVAILABLE, "transient error")
            return "success"

        res = interceptor.intercept_unary_unary(
            flaky_continuation, rpc.ClientCallDetails(method="test"), None
        )
        self.assertEqual("success", res)
        self.assertEqual(3, attempts[0])

        def not_found_continuation(_details, _req):
            raise rpc.RpcError(rpc.StatusCode.NOT_FOUND, "not found")

        with self.assertRaises(rpc.RpcError) as ctx:
            interceptor.intercept_unary_unary(
                not_found_continuation, rpc.ClientCallDetails(method="test"), None
            )
        self.assertEqual(rpc.StatusCode.NOT_FOUND, ctx.exception.code())

    def test_rqd_modules_work_without_grpc(self):
        with mock.patch.dict(sys.modules, {"grpc": None}):
            self.assertIsNone(sys.modules.get("grpc"))
            rqd_core = mock.MagicMock()
            servicer = rqd.rqdservicers.RqdInterfaceServicer(rqd_core)
            self.assertTrue(hasattr(servicer, "ReportStatus"))
            self.assertTrue(hasattr(rqd.cuerqd, "RqdInterfaceStub"))
            self.assertTrue(hasattr(rqd.rqnetwork, "RetryOnRpcErrorClientInterceptor"))


if __name__ == "__main__":
    unittest.main()
