#!/usr/bin/env python3
"""Verify the slskd REST calls match slskd's real routes/DTOs.

Downloads silently did nothing because enqueue posted to a non-existent
`/downloads` route with the wrong body. These asserts pin the contract.

Run:  python tools/test_soulseek_api.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import soulseek

calls = []


def fake_request(method, path, json_body=None, timeout=30.0):
    calls.append((method, path, json_body))
    return {"id": "x"} if method == "POST" and path == "/searches" else None


soulseek._request = fake_request

soulseek.enqueue_download("Some User", [{"filename": "a\\b.flac", "size": 123}])
method, path, body = calls[-1]
assert method == "POST", method
assert path == "/transfers/downloads/Some%20User", path
assert body == [{"filename": "a\\b.flac", "size": 123}], body

soulseek.rescan_shares()
assert calls[-1][:2] == ("PUT", "/shares"), calls[-1][:2]

soulseek.search("q", timeout_ms=45000)
method, path, body = calls[-1]
assert body.get("searchTimeout") == 45000, body
assert "timeout" not in body, body

print("ok")
