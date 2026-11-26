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

"""Integration tests for PyCue client stubs and retry interceptors with `rpc`."""

# pylint: disable=too-few-public-methods

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from concurrent import futures
import subprocess
import sys
import unittest

from opencue_proto import cue_pb2
from opencue_proto import rpc
import opencue
import opencue.cuebot


class _DummyCueServicer(object):
    def __init__(self):
        self.attempts = 0

    def GetSystemStats(self, _request, _context):
        self.attempts += 1
        return cue_pb2.CueGetSystemStatsResponse(
            stats=cue_pb2.SystemStats(manage_threads=9)
        )


class PyCueRpcIntegrationTests(unittest.TestCase):
    """Integration tests verifying PyCue client stubs and interceptors."""

    def test_cuebot_service_map_and_stub_invocation(self):
        real_stub_cls = rpc.stub_factory(cue_pb2, "CueInterface")
        orig_cue_stub = opencue.cuebot.Cuebot.SERVICE_MAP.get("cue")
        opencue.cuebot.Cuebot.SERVICE_MAP["cue"] = real_stub_cls

        servicer = _DummyCueServicer()
        srv = rpc.server(futures.ThreadPoolExecutor(max_workers=4))
        srv.add_service(cue_pb2, "CueInterface", servicer)
        port = srv.add_insecure_port("127.0.0.1:0")
        srv.start()

        channel = rpc.insecure_channel("127.0.0.1:%d" % port)
        try:
            cue_stub_cls = opencue.cuebot.Cuebot.SERVICE_MAP["cue"]
            stub = cue_stub_cls(channel)
            resp = stub.GetSystemStats(cue_pb2.CueGetSystemStatsRequest(), timeout=5)
            self.assertEqual(9, resp.stats.manage_threads)
            self.assertEqual(1, servicer.attempts)
        finally:
            channel.close()
            srv.stop(0)
            if orig_cue_stub is not None:
                opencue.cuebot.Cuebot.SERVICE_MAP["cue"] = orig_cue_stub

    def test_retry_interceptor_retries_unavailable(self):
        attempts = [0]
        interceptor = opencue.cuebot.RetryOnRpcErrorClientInterceptor(
            max_attempts=4,
            sleeping_policy=opencue.cuebot.ExponentialBackoff(
                init_backoff_ms=1, max_backoff_ms=5, multiplier=2
            ),
            status_for_retry=(rpc.StatusCode.UNAVAILABLE,),
        )

        def continuation(_details, _req):
            attempts[0] += 1
            if attempts[0] < 3:
                raise rpc.RpcError(rpc.StatusCode.UNAVAILABLE, "temporary issue")
            return "ok"

        res = interceptor.intercept_unary_unary(
            continuation, rpc.ClientCallDetails(method="test"), None
        )
        self.assertEqual("ok", res)
        self.assertEqual(3, attempts[0])

    def test_retry_interceptor_raises_on_non_retryable(self):
        interceptor = opencue.cuebot.RetryOnRpcErrorClientInterceptor(
            max_attempts=3,
            sleeping_policy=opencue.cuebot.ExponentialBackoff(
                init_backoff_ms=1, max_backoff_ms=5, multiplier=2
            ),
            status_for_retry=(rpc.StatusCode.UNAVAILABLE,),
        )

        def continuation(_details, _req):
            raise rpc.RpcError(rpc.StatusCode.NOT_FOUND, "missing resource")

        with self.assertRaises(rpc.RpcError) as ctx:
            interceptor.intercept_unary_unary(
                continuation, rpc.ClientCallDetails(method="test"), None
            )
        self.assertEqual(rpc.StatusCode.NOT_FOUND, ctx.exception.code())

    def test_works_without_grpc_module(self):
        code = (
            "import sys; sys.modules['grpc'] = None; "
            "import opencue.cuebot; "
            "assert 'cue' in opencue.cuebot.Cuebot.SERVICE_MAP; "
            "assert sys.modules.get('grpc') is None"
        )
        res = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, res.returncode, res.stderr)


if __name__ == "__main__":
    unittest.main()
