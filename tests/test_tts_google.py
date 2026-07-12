"""Google Cloud TTS synthesize path (REST), exercised without the network.

We monkeypatch the token fetch and ``requests.post`` so these stay hermetic;
the live call is verified manually against the real API. The point here is to
pin the request shape (endpoint, voice/language/encoding, quota-project header)
and the error → ``TTSUnavailable`` mapping the prefetch worker relies on.
"""

import base64

import pytest

from kana_quiz import tts


class _FakeResp:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _patch_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts, "_access_token", lambda: "fake-token")
    monkeypatch.delenv("KANA_TTS_DISABLED", raising=False)


def test_synthesize_posts_expected_request_and_decodes_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_token(monkeypatch)
    monkeypatch.setattr(tts, "GCP_PROJECT", "test-project")
    captured: dict = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        audio = base64.b64encode(b"\xff\xf3fake-mp3").decode()
        return _FakeResp(200, {"audioContent": audio})

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)

    out = tts.synthesize("ねこ", voice="ja-JP-Chirp3-HD-Aoede", model="google-chirp3-hd")
    assert out == b"\xff\xf3fake-mp3"
    assert captured["url"].endswith("text:synthesize")
    assert captured["headers"]["Authorization"] == "Bearer fake-token"
    assert captured["headers"]["x-goog-user-project"] == "test-project"
    assert captured["json"]["voice"] == {
        "languageCode": "ja-JP",
        "name": "ja-JP-Chirp3-HD-Aoede",
    }
    assert captured["json"]["audioConfig"]["audioEncoding"] == "MP3"
    assert captured["json"]["input"]["text"] == "ねこ"


def test_synthesize_disabled_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KANA_TTS_DISABLED", "1")
    with pytest.raises(tts.TTSUnavailable):
        tts.synthesize("ねこ", voice="v", model="m")


def test_synthesize_auth_error_maps_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_token(monkeypatch)

    def _fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResp(403, text="permission denied")

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)
    with pytest.raises(tts.TTSUnavailable):
        tts.synthesize("ねこ", voice="v", model="m")


def test_synthesize_server_error_raises_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_token(monkeypatch)

    def _fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResp(500, text="backend boom")

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)
    with pytest.raises(RuntimeError) as exc:
        tts.synthesize("ねこ", voice="v", model="m")
    assert not isinstance(exc.value, tts.TTSUnavailable)
