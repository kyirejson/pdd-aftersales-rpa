import json

from pdd_rpa.yto_credentials import load_yto_credentials, save_yto_credentials


def test_yto_credentials_are_encrypted_and_round_trip(tmp_path):
    path = tmp_path / "yto.dpapi"

    save_yto_credentials(
        path,
        "account-sentinel",
        "password-sentinel",
        protect=lambda value: b"protected:" + value[::-1],
    )

    raw = path.read_text(encoding="utf-8")
    assert "account-sentinel" not in raw
    assert "password-sentinel" not in raw
    assert json.loads(raw)["protection"] == "windows-dpapi-current-user"
    loaded = load_yto_credentials(
        path,
        unprotect=lambda value: value.removeprefix(b"protected:")[::-1],
    )
    assert loaded.username == "account-sentinel"
    assert loaded.password == "password-sentinel"
