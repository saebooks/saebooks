"""RemoteLodgementService.poll_status is a documented, gated stub.

The real ATO status-retrieval (ebMS3 response retrieval) and the
lodge-server status route are NOT yet contracted — gated on the PVT pack.
Until then the remote client must fail loudly rather than fabricate a Pull
call or a fake HTTP route. This test pins that behaviour so a future
implementor sees a clear RED if they wire a real route without updating the
contract + this test.
"""
from __future__ import annotations

import pytest

from saebooks.services.lodgement import RemoteLodgementService


async def test_poll_status_is_gated_stub() -> None:
    svc = RemoteLodgementService(submitter_abn="12345678901")
    with pytest.raises(NotImplementedError) as ei:
        await svc.poll_status(receipt_ref="payevent-1", product="stp")
    # message names the gate so the failure is self-explanatory
    msg = str(ei.value).lower()
    assert "pvt" in msg or "not yet" in msg or "not contracted" in msg
