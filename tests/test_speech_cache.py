from jarvis.speech_cache import SpeechCache


def test_audio_cache_is_bounded_expires_and_never_stores_plaintext_keys():
    now = [0]
    cache = SpeechCache(max_bytes=8, max_entries=2, ttl=10, clock=lambda: now[0])
    cache.put("private first phrase", b"1234")
    cache.put("second", b"5678")
    assert cache.get("private first phrase") == b"1234"
    cache.put("third", b"abcd")
    assert cache.get("second") is None
    assert cache.bytes == 8 and len(cache.items) == 2
    assert all(isinstance(key, bytes) and len(key) == 32 for key in cache.items)
    now[0] = 11
    assert cache.get("private first phrase") is None
    cache.put("too large", b"x" * 9)
    assert cache.get("too large") is None
    cache.put("third", b"z")
    assert cache.bytes == 1
