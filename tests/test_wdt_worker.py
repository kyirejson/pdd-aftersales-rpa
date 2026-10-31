from __future__ import annotations

import io
import json

from pdd_rpa import wdt_worker


def test_worker_protocol_escapes_zero_width_characters(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(wdt_worker.sys, "stdout", stream)

    wdt_worker._emit({"window_title": "Microsoft\u200b Edge"})

    payload = stream.getvalue()
    assert payload.isascii()
    assert json.loads(payload)["window_title"] == "Microsoft\u200b Edge"
