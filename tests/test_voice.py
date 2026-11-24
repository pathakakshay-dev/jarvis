import os
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jarvis.config import load_settings
from jarvis.main import create_app
from jarvis.safety.halt import HaltController
from jarvis.voice.errors import TranscriptionError, VoiceCancelled
from jarvis.voice.gate import VoiceGate
from jarvis.voice.speak import EDGE_VOICE_EN, EDGE_VOICE_HI, Speech, prepare_speech, voice_for
from jarvis.voice.diagnostic import CONSENT_HEADER
from jarvis.voice.transcribe import (
    MAX_AUDIO_BYTES,
    FasterModel,
    WhisperTranscriber,
    inspect_audio,
    install_av_open_compat,
    english_text_is_clearer,
    language_options,
    needs_english_retry,
    trim_leading_spike,
)
from tests.test_api import _settings


class FakeTranscriber:
    def __init__(self, text="hello from the mic", error=None):
        self.text = text
        self.error = error
        self.calls = []

    def transcribe(self, audio, content_type, halt, gate, generation):
        self.calls.append(bytes(audio))
        if not gate.current(generation, halt.halted):
            raise VoiceCancelled("cancelled")
        if self.error:
            raise TranscriptionError(self.error)
        return self.text


class FakeSpeaker:
    def __init__(self, engine="edge", audio=b"ID3test"):
        self.engine = engine
        self.audio = audio
        self.calls = []

    async def speak(self, text, halt, gate, generation):
        self.calls.append(text)
        if not gate.current(generation, halt.halted):
            raise VoiceCancelled("cancelled")
        if self.engine == "sapi":
            return Speech(engine="sapi", text=text, audio=None, media_type="", notice="Windows speech")
        return Speech(engine="edge", text=text, audio=self.audio, media_type="audio/mpeg", notice="Edge")


def _app(tmp_path: Path, transcriber=None, speaker=None):
    return create_app(
        settings=_settings(tmp_path),
        start_hotkeys_flag=False,
        transcriber=transcriber or FakeTranscriber(),
        speaker=speaker or FakeSpeaker(),
    )


def test_audio_is_transcribed_locally_and_not_stored_as_chat(tmp_path: Path):
    transcriber = FakeTranscriber(text="I prefer short answers")
    app = _app(tmp_path, transcriber=transcriber)
    with TestClient(app) as client:
        response = client.post(
            "/api/voice/transcribe",
            content=b"webm-bytes",
            headers={"content-type": "audio/webm"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["text"] == "I prefer short answers"
        assert body["audio_leaves_machine"] is False
        assert "not sent" in body["notice"].lower() or "not sent" in body["notice"]
        assert transcriber.calls == [b"webm-bytes"]
        assert client.get("/api/messages").json()["messages"] == []
        voice = client.get("/api/status").json()["voice"]
        assert voice["audio_leaves_machine"] is False
        assert voice["transcription"] == "local-whisper-small"
        assert "Whisper small" in body["notice"]
        assert voice["microphone"] == "push-to-talk"
        assert "Microsoft" in voice["speech_notice"]


def test_empty_and_oversized_audio_never_reach_the_model(tmp_path: Path):
    transcriber = FakeTranscriber()
    app = _app(tmp_path, transcriber=transcriber)
    with TestClient(app) as client:
        empty = client.post("/api/voice/transcribe", content=b"", headers={"content-type": "audio/webm"})
        assert empty.status_code == 400
        huge = client.post(
            "/api/voice/transcribe",
            content=b"x" * (MAX_AUDIO_BYTES + 1),
            headers={"content-type": "audio/webm"},
        )
        assert huge.status_code == 400
    assert transcriber.calls == []


def test_transcription_errors_are_reported(tmp_path: Path):
    app = _app(tmp_path, transcriber=FakeTranscriber(error="The recording could not be read."))
    with TestClient(app) as client:
        response = client.post(
            "/api/voice/transcribe",
            content=b"bad",
            headers={"content-type": "audio/webm"},
        )
        assert response.status_code == 502
        assert "could not be read" in response.json()["detail"]


def test_page_requires_a_press_and_explains_microphone_denial(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        html = client.get("/").text
    constraints = html[html.index("function captureConstraints") : html.index("function stopLevel")]
    refresh = html[html.index("async function refreshMics") : html.index("function releaseMicrophone")]
    recording = html[html.index("async function startRecording") : html.index("function stopRecording")]
    assert "echoCancellation: false" in constraints
    assert "deviceId" in constraints
    assert "getUserMedia" not in constraints
    assert refresh.count("getUserMedia") == 1
    assert recording.count("getUserMedia") == 2
    assert html.count("getUserMedia") == 3
    assert 'id="mic-device"' in html
    assert 'id="mic-level"' in html
    assert "Microphone permission was denied" in html
    assert "No microphone was found" in html
    assert "Hold to talk" in html
    handler = html[html.index("async function sendAudio") : html.index("function cancelVoiceDraft")]
    assert "response.status === 504" in handler
    assert 'getElementById("voice-use").hidden = false' in handler
    assert "Whisper small" in html
    assert 'id="diagnostic-keep"' in html
    assert "Keep clip for diagnosis" in html
    assert "Compare decoding" in html
    assert "Delete diagnostic audio" in html
    assert "X-Jarvis-Diagnostic-Consent" in html
    assert "keep-local" in html
    boot = html[html.rindex("loadMessages();") :]
    assert "keep-local" not in boot
    assert "/api/voice/diagnostic/compare" not in boot
    assert 'id="voice-mute" type="checkbox" checked' in html
    for state in ("Idle", "Recording", "Processing", "Speaking", "Stopped", "Error"):
        assert state in html
    boot = html[html.rindex("loadMessages();") :]
    assert "getUserMedia" not in boot
    assert "speakText" not in boot
    load_fn = html[html.index("async function loadMessages") : html.index('getElementById("emergency-stop")')]
    assert "speakText" not in load_fn
    resume = html[html.index('getElementById("resume")') : html.index('getElementById("chat-form")')]
    assert "speakText" not in resume
    assert "getUserMedia" not in resume


def test_voice_output_play_stop_and_local_fallback(tmp_path: Path):
    speaker = FakeSpeaker()
    app = _app(tmp_path, speaker=speaker)
    with TestClient(app) as client:
        spoken = client.post("/api/voice/speak", json={"text": "Hello"})
        assert spoken.status_code == 200
        assert spoken.headers["x-voice-engine"] == "edge"
        assert spoken.content == b"ID3test"
        assert "no-store" in spoken.headers["cache-control"]
        generation = app.state.voice_gate.capture()
        stopped = client.post("/api/voice/stop")
        assert stopped.json() == {"stopped": True, "replayed": False}
        assert app.state.voice_gate.current(generation, False) is False

    fallback = FakeSpeaker(engine="sapi")
    app = _app(tmp_path, speaker=fallback)
    with TestClient(app) as client:
        local = client.post("/api/voice/speak", json={"text": "नमस्ते"})
        assert local.status_code == 200
        assert local.json()["engine"] == "sapi"
        assert local.json()["replayed"] is False
        assert local.json()["text"] == "नमस्ते"
    assert fallback.calls == ["नमस्ते"]


def test_emergency_stop_cancels_voice_and_resume_does_not_replay(tmp_path: Path):
    speaker = FakeSpeaker()
    transcriber = FakeTranscriber()
    app = _app(tmp_path, transcriber=transcriber, speaker=speaker)
    with TestClient(app) as client:
        generation = app.state.voice_gate.capture()
        stopped = client.post("/api/emergency-stop")
        assert stopped.json() == {"requested": True}
        assert app.state.voice_gate.current(generation, True) is False
        refused_audio = client.post(
            "/api/voice/transcribe",
            content=b"clip",
            headers={"content-type": "audio/webm"},
        )
        refused_speech = client.post("/api/voice/speak", json={"text": "say this later"})
        assert refused_audio.status_code == 409
        assert refused_speech.status_code == 409
        assert transcriber.calls == []
        assert speaker.calls == []
        resumed = client.post("/api/resume")
        assert resumed.json()["replayed"] is False
        assert speaker.calls == []
        fresh = client.post("/api/voice/speak", json={"text": "fresh reply"})
        assert fresh.status_code == 200
        assert speaker.calls == ["fresh reply"]


def test_restart_does_not_replay_voice(tmp_path: Path):
    settings = _settings(tmp_path)
    first = create_app(settings=settings, start_hotkeys_flag=False, transcriber=FakeTranscriber(), speaker=FakeSpeaker())
    first.state.voice_gate.cancel()
    first.state.store.close()
    second = create_app(settings=settings, start_hotkeys_flag=False, transcriber=FakeTranscriber(), speaker=FakeSpeaker())
    assert second.state.voice_gate.generation == 0
    assert not (settings.data_dir / "voice-queue.json").exists()
    second.state.store.close()


def test_cancel_discards_a_loaded_transcription():
    class Model:
        def __init__(self):
            self.closed = False

        def transcribe(self, audio, suffix):
            return "kept"

        def close(self):
            self.closed = True

    created = []

    def factory(device, compute):
        model = Model()
        created.append(model)
        return model

    transcriber = WhisperTranscriber(model_factory=factory, device="cpu", cuda_probe=lambda: False)
    halt = HaltController()
    gate = VoiceGate()
    generation = gate.capture()
    gate.cancel()
    with pytest.raises(VoiceCancelled):
        transcriber.transcribe(b"audio", "audio/webm", halt, gate, generation)
    assert created == []


def test_cuda_failure_uses_cpu_and_drops_the_model():
    closed = []

    class Model:
        def transcribe(self, audio, suffix):
            return "cpu text"

        def close(self):
            closed.append(True)

    def factory(device, compute):
        if device == "cuda":
            raise RuntimeError("cuda unavailable")
        return Model()

    transcriber = WhisperTranscriber(model_factory=factory, device="auto", cuda_probe=lambda: True)
    text = transcriber.transcribe(b"audio", "audio/webm", HaltController(), VoiceGate(), 0)
    assert text == "cpu text"
    assert closed == [True]


def test_transcription_audio_file_is_removed(tmp_path: Path):
    inner = SimpleNamespace(path="")

    def transcribe(path, **kwargs):
        inner.path = path
        assert os.path.exists(path)
        assert Path(path).suffix == ".webm"
        return [SimpleNamespace(text=" hello ")], None

    inner.transcribe = transcribe
    assert FasterModel(inner).transcribe(b"abc", ".webm") == "hello"
    assert inner.path
    assert not os.path.exists(inner.path)


def test_voice_selection_and_length():
    assert voice_for("Hello") == EDGE_VOICE_EN
    assert voice_for("नमस्ते") == EDGE_VOICE_HI
    text, shortened = prepare_speech("word " * 400)
    assert shortened is True
    assert len(text) <= 1200


def _silence_wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    return path


def test_recorded_wav_reports_duration_level_and_silence(tmp_path: Path):
    path = _silence_wav(tmp_path / "clip.wav")
    measured = inspect_audio(path.read_bytes(), "audio/wav")
    assert measured["sample_rate"] == 16000
    assert measured["channels"] == 1
    assert measured["codec"] == "pcm_s16le"
    assert measured["decoded_seconds"] == 0.1
    assert measured["peak"] == 0
    assert measured["silence"] == 1
    assert not (tmp_path / "clip.wav").with_suffix(".kept").exists()


def test_leading_click_is_dropped_and_speech_at_the_start_is_kept():
    import numpy as np

    rate = 16000
    click = np.zeros(rate, dtype=np.float32)
    click[:800] = 0.7
    click[int(0.30 * rate) : int(0.70 * rate)] = 0.2
    trimmed = trim_leading_spike(click)
    assert trimmed.size < click.size
    assert np.max(np.abs(trimmed[:800])) < 0.05
    assert np.max(np.abs(trimmed)) == pytest.approx(0.2)
    immediate = np.full(rate, 0.2, dtype=np.float32)
    assert trim_leading_spike(immediate).size == immediate.size


def test_language_choice_follows_the_top_score_between_english_and_hindi():
    confident_english = language_options([("en", 0.76), ("hi", 0.18), ("ta", 0.02)])
    assert confident_english == {"language": "en"}
    assert "hotwords" not in confident_english
    english_below_old_floor = language_options([("en", 0.59), ("pt", 0.3), ("hi", 0.08)])
    assert english_below_old_floor == {"language": "en"}
    english_with_a_close_hindi_score = language_options([("en", 0.7), ("hi", 0.55), ("ur", 0.04)])
    assert english_with_a_close_hindi_score == {"language": "en"}
    hindi = language_options([("hi", 0.93), ("ur", 0.03), ("en", 0.02)])
    assert hindi == {"language": "hi"}
    urdu_leading = language_options([("ur", 0.55), ("hi", 0.2), ("en", 0.1)])
    assert urdu_leading == {"language": "hi"}
    portuguese = language_options([("pt", 0.62), ("en", 0.2), ("hi", 0.1)])
    assert portuguese == {"language": "hi"}
    tied = language_options([("en", 0.5), ("hi", 0.5)])
    assert tied == {"language": "hi"}
    assert language_options([("fr", 0.2), ("de", 0.2)]) == {"language": "hi"}
    assert language_options([]) == {"language": "hi"}


def test_accented_english_is_retried_and_a_hindi_sentence_is_not():
    assert needs_english_retry([("hi", 0.475), ("ur", 0.253), ("en", 0.157)])
    assert not needs_english_retry([("hi", 0.93), ("ur", 0.03), ("en", 0.02)])
    assert not needs_english_retry([("en", 0.7), ("hi", 0.2)])
    assert not needs_english_retry([("pt", 0.62), ("en", 0.2), ("hi", 0.1)])
    phonetic = "आलो जार्विस माइने मी चब्षें"
    english = "Hello Jarvis, my name is Shaksha"
    assert english_text_is_clearer(phonetic, english)
    assert not english_text_is_clearer("मुझे 10 बजे सोना है", "Hello Jarvis my name is Akshay")
    assert not english_text_is_clearer("मेरा नाम अक्षय है", english)


def test_hindi_detection_of_english_words_uses_the_english_text(tmp_path: Path):
    class Probe:
        def __init__(self):
            self.calls = []

        def detect_language(self, audio, vad_filter=False, vad_parameters=None):
            return "hi", 0.475, [("hi", 0.475), ("ur", 0.253), ("en", 0.157)]

        def transcribe(self, audio, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("language") == "en":
                return [SimpleNamespace(text="Hello Jarvis, my name is Shaksha")], None
            return [SimpleNamespace(text="आलो जार्विस माइने मी चब्षें")], None

        def close(self):
            return None

    probe = Probe()
    heard = FasterModel(probe).transcribe(_silence_wav(tmp_path / "clip.wav").read_bytes(), ".wav")
    assert heard == "Hello Jarvis, my name is Shaksha"
    assert [call["language"] for call in probe.calls] == ["hi", "en"]
    assert all(call["vad_filter"] is False and "hotwords" not in call for call in probe.calls)


def test_a_real_hindi_sentence_stays_hindi_even_if_english_also_scores(tmp_path: Path):
    class Probe:
        def __init__(self):
            self.calls = []

        def detect_language(self, audio, vad_filter=False, vad_parameters=None):
            return "hi", 0.62, [("hi", 0.62), ("en", 0.18)]

        def transcribe(self, audio, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("language") == "en":
                return [SimpleNamespace(text="Hello Jarvis my name is Akshay")], None
            return [SimpleNamespace(text="मुझे 10 बजे सोना है")], None

        def close(self):
            return None

    probe = Probe()
    heard = FasterModel(probe).transcribe(_silence_wav(tmp_path / "clip.wav").read_bytes(), ".wav")
    assert heard == "मुझे 10 बजे सोना है"
    assert [call["language"] for call in probe.calls] == ["hi", "en"]


def test_decoder_settings_follow_the_detected_language(tmp_path: Path):
    class Probe:
        def __init__(self):
            self.kwargs = None

        def detect_language(self, audio, vad_filter=False, vad_parameters=None):
            return "en", 0.8, [("en", 0.8), ("hi", 0.1)]

        def transcribe(self, audio, **kwargs):
            self.kwargs = kwargs
            return [SimpleNamespace(text="ok")], None

        def close(self):
            return None

    probe = Probe()
    assert FasterModel(probe).transcribe(_silence_wav(tmp_path / "clip.wav").read_bytes(), ".wav") == "ok"
    assert probe.kwargs["beam_size"] == 5
    assert probe.kwargs["vad_filter"] is False
    assert "vad_parameters" not in probe.kwargs
    assert probe.kwargs["language"] == "en"
    assert "hotwords" not in probe.kwargs


def test_english_below_the_old_floor_stays_on_the_english_decoder(tmp_path: Path):
    class Probe:
        def __init__(self):
            self.kwargs = None

        def detect_language(self, audio, vad_filter=False, vad_parameters=None):
            return "en", 0.59, [("en", 0.59), ("hi", 0.22), ("pt", 0.1)]

        def transcribe(self, audio, **kwargs):
            self.kwargs = kwargs
            return [SimpleNamespace(text="ok")], None

        def close(self):
            return None

    probe = Probe()
    FasterModel(probe).transcribe(_silence_wav(tmp_path / "clip.wav").read_bytes(), ".wav")
    assert probe.kwargs["language"] == "en"
    assert "hotwords" not in probe.kwargs


def test_portuguese_detection_is_decoded_as_hindi(tmp_path: Path):
    class Probe:
        def __init__(self):
            self.kwargs = None

        def detect_language(self, audio, vad_filter=False, vad_parameters=None):
            return "pt", 0.62, [("pt", 0.62), ("en", 0.2), ("hi", 0.1)]

        def transcribe(self, audio, **kwargs):
            self.kwargs = kwargs
            return [SimpleNamespace(text="ok")], None

        def close(self):
            return None

    probe = Probe()
    assert FasterModel(probe).transcribe(_silence_wav(tmp_path / "clip.wav").read_bytes(), ".wav") == "ok"
    assert probe.kwargs["language"] == "hi"
    assert probe.kwargs["vad_filter"] is False
    assert "vad_parameters" not in probe.kwargs
    assert "hotwords" not in probe.kwargs


def test_pyav_metadata_errors_argument_is_ignored_for_real_audio(tmp_path: Path):
    import av

    path = _silence_wav(tmp_path / "clip.wav")
    original = getattr(av.open, "_jarvis_original", av.open)
    with pytest.raises(TypeError, match="metadata_errors"):
        original(str(path), mode="r", metadata_errors="ignore")
    install_av_open_compat()
    with av.open(str(path), mode="r", metadata_errors="ignore") as container:
        frames = list(container.decode(audio=0))
    assert frames


def test_transcription_failure_reports_the_underlying_error(tmp_path: Path):
    class Broken:
        def transcribe(self, audio, suffix):
            raise RuntimeError("decoder rejected the recording")

        def close(self):
            return None

    transcriber = WhisperTranscriber(model_factory=lambda device, compute: Broken(), device="cpu")
    with pytest.raises(TranscriptionError, match="decoder rejected the recording"):
        transcriber.transcribe(b"audio", "audio/wav", HaltController(), VoiceGate(), 0)


def test_diagnostic_audio_requires_consent_and_can_be_deleted(tmp_path: Path):
    settings = _settings(tmp_path)
    leftover = settings.data_dir / "diagnostic"
    leftover.mkdir(parents=True)
    (leftover / "clip.webm").write_bytes(b"old")
    (leftover / "clip.json").write_text('{"content_type": "audio/webm", "bytes": 3}', encoding="utf-8")
    app = _app(tmp_path)
    assert not (leftover / "clip.webm").exists()
    with TestClient(app) as client:
        refused = client.post("/api/voice/diagnostic", content=b"mic", headers={"content-type": "audio/webm"})
        assert refused.status_code == 400
        assert not (leftover / "clip.webm").exists()
        missing = client.post("/api/voice/diagnostic/compare")
        assert missing.status_code == 404
        transcribed = client.post("/api/voice/transcribe", content=b"mic", headers={"content-type": "audio/webm"})
        assert transcribed.status_code == 200
        assert not (leftover / "clip.webm").exists()
        empty = client.post(
            "/api/voice/diagnostic",
            content=b"",
            headers={"content-type": "audio/webm", "X-Jarvis-Diagnostic-Consent": CONSENT_HEADER},
        )
        assert empty.status_code == 400
        kept = client.post(
            "/api/voice/diagnostic",
            content=b"mic",
            headers={"content-type": "audio/webm", "X-Jarvis-Diagnostic-Consent": CONSENT_HEADER},
        )
        assert kept.status_code == 200
        assert kept.json()["audio_leaves_machine"] is False
        assert "not sent" in kept.json()["notice"]
        assert (leftover / "clip.webm").read_bytes() == b"mic"
        assert client.get("/api/voice/diagnostic").json()["stored"] is True
        removed = client.delete("/api/voice/diagnostic")
        assert removed.json()["deleted"] is True
        assert removed.json()["audio_leaves_machine"] is False
        assert not (leftover / "clip.webm").exists()
        assert client.get("/api/voice/diagnostic").json()["stored"] is False


def test_diagnostic_comparison_runs_locally_and_deletes_the_clip(tmp_path: Path):
    class Comparer:
        def __init__(self):
            self.audio = b""

        def compare(self, audio, content_type, halt, gate, generation):
            self.audio = bytes(audio)
            if not gate.current(generation, halt.halted):
                raise VoiceCancelled("cancelled")
            return [
                {
                    "id": item,
                    "label": item,
                    "text": "local " + item,
                    "error": "",
                    "detected_language": "hi",
                    "detected_probability": 0.73,
                    "elapsed_s": 1.5,
                    "settings": {
                        "language": "hi" if item in {"B", "D"} else None,
                        "hotwords": "हिंदी English" if item == "A" else None,
                        "beam_size": 5,
                        "vad_filter": item != "D",
                        "condition_on_previous_text": False,
                    },
                }
                for item in ("A", "B", "C", "D")
            ]

    comparer = Comparer()
    app = _app(tmp_path, transcriber=comparer)
    with TestClient(app) as client:
        kept = client.post(
            "/api/voice/diagnostic",
            content=b"same-clip",
            headers={"content-type": "audio/webm", "X-Jarvis-Diagnostic-Consent": CONSENT_HEADER},
        )
        assert kept.status_code == 200
        compared = client.post("/api/voice/diagnostic/compare")
        assert compared.status_code == 200
        body = compared.json()
        assert body["audio_leaves_machine"] is False
        assert body["deleted"] is True
        assert "deleted" in body["notice"]
        assert [run["id"] for run in body["runs"]] == ["A", "B", "C", "D"]
        assert [run["text"] for run in body["runs"]] == ["local A", "local B", "local C", "local D"]
        assert comparer.audio == b"same-clip"
        assert client.get("/api/voice/diagnostic").json()["stored"] is False
        stopped = client.post(
            "/api/voice/diagnostic",
            content=b"again",
            headers={"content-type": "audio/webm", "X-Jarvis-Diagnostic-Consent": CONSENT_HEADER},
        )
        assert stopped.status_code == 200
        halt = client.post("/api/emergency-stop")
        assert halt.json() == {"requested": True}
        assert client.get("/api/voice/diagnostic").json()["stored"] is False
        blocked = client.post(
            "/api/voice/diagnostic",
            content=b"later",
            headers={"content-type": "audio/webm", "X-Jarvis-Diagnostic-Consent": CONSENT_HEADER},
        )
        assert blocked.status_code == 409


def test_same_clip_is_decoded_four_ways_without_changing_production(tmp_path: Path):
    class Probe:
        def __init__(self):
            self.calls = []

        def detect_language(self, audio, vad_filter=False, vad_parameters=None):
            self.calls.append(("detect", vad_filter))
            return "hi", 0.73, [("hi", 0.73), ("en", 0.2)]

        def transcribe(self, audio, **kwargs):
            self.calls.append(kwargs)
            return [SimpleNamespace(text="heard")], None

        def close(self):
            return None

    probe = Probe()
    audio = _silence_wav(tmp_path / "clip.wav").read_bytes()
    runs = FasterModel(probe).compare(audio, ".wav", HaltController(), VoiceGate(), 0)
    assert [run["id"] for run in runs] == ["A", "B", "C", "D"]
    assert runs[0]["detected_language"] == "hi"
    assert runs[0]["detected_probability"] == 0.73
    assert runs[0]["settings"]["language"] == "hi"
    assert runs[0]["settings"]["hotwords"] is None
    assert runs[0]["settings"]["vad_filter"] is False
    assert runs[0]["settings"]["beam_size"] == 5
    assert runs[1]["settings"]["language"] == "hi"
    assert runs[1]["settings"]["hotwords"] is None
    assert runs[2]["settings"]["language"] is None
    assert runs[2]["settings"]["hotwords"] is None
    assert runs[3]["settings"]["language"] == "hi"
    assert runs[3]["settings"]["vad_filter"] is False
    assert runs[3]["settings"]["beam_size"] == 5
    assert all(run["settings"]["condition_on_previous_text"] is False for run in runs)
    assert all(run["elapsed_s"] >= 0 for run in runs)
    transcribes = [call for call in probe.calls if isinstance(call, dict)]
    assert len(transcribes) == 4
    assert transcribes[0]["language"] == "hi"
    assert transcribes[0]["vad_filter"] is False
    assert "vad_parameters" not in transcribes[0]
    assert transcribes[0]["beam_size"] == 5
    assert "hotwords" not in transcribes[0]
    assert transcribes[1]["vad_filter"] is True
    assert "hotwords" not in transcribes[1]
    assert transcribes[1]["language"] == "hi"
    assert "language" not in transcribes[2]
    assert "hotwords" not in transcribes[2]
    assert transcribes[3]["language"] == "hi"
    assert transcribes[3]["vad_filter"] is False
    assert "vad_parameters" not in transcribes[3]
    assert transcribes[3]["beam_size"] == 5
    detects = [call for call in probe.calls if isinstance(call, tuple)]
    assert detects == [("detect", True), ("detect", True), ("detect", True), ("detect", False)]
    gate = VoiceGate()
    generation = gate.capture()
    gate.cancel()
    with pytest.raises(VoiceCancelled):
        FasterModel(probe).compare(audio, ".wav", HaltController(), gate, generation)


def test_voice_timeout_matches_small_model_cpu_time(tmp_path: Path):
    assert load_settings(tmp_path, {}).voice_timeout_s == 180
    assert load_settings(tmp_path, {"JARVIS_VOICE_TIMEOUT_S": "999"}).voice_timeout_s == 180
    assert load_settings(tmp_path, {"JARVIS_VOICE_TIMEOUT_S": "90"}).voice_timeout_s == 90


def test_transcription_timeout_keeps_the_clip_and_blocks_a_second_model(tmp_path: Path):
    class Slow:
        def __init__(self):
            self.started = threading.Event()
            self.calls = 0

        def transcribe(self, audio, content_type, halt, gate, generation):
            self.calls += 1
            if self.calls > 1:
                return "late text"
            self.started.set()
            time.sleep(0.8)
            if not gate.current(generation, halt.halted):
                raise VoiceCancelled("cancelled")
            return "late text"

    settings = _settings(tmp_path)
    settings.voice_timeout_s = 0.2
    slow = Slow()
    app = create_app(settings=settings, start_hotkeys_flag=False, transcriber=slow, speaker=FakeSpeaker())
    with TestClient(app) as client:
        timed = client.post("/api/voice/transcribe", content=b"clip", headers={"content-type": "audio/webm"})
        assert timed.status_code == 504
        assert "still in the page" in timed.json()["detail"]
        assert "discarded" not in timed.json()["detail"].lower()
        assert slow.started.is_set()
        busy = client.post("/api/voice/transcribe", content=b"clip", headers={"content-type": "audio/webm"})
        assert busy.status_code == 409
        for _ in range(40):
            if not app.state.halt.busy:
                break
            time.sleep(0.05)
        assert app.state.halt.busy is False
        ready = client.post("/api/voice/transcribe", content=b"clip", headers={"content-type": "audio/webm"})
        assert ready.status_code == 200
        assert ready.json()["text"] == "late text"


def test_whisper_stays_on_the_small_model(tmp_path: Path):
    settings = load_settings(tmp_path, {"JARVIS_WHISPER_DEVICE": "large", "JARVIS_WHISPER_MODEL": "medium"})
    assert settings.whisper_model == "small"
    assert settings.whisper_device == "auto"
    chosen = load_settings(tmp_path, {"JARVIS_WHISPER_DEVICE": "cpu", "JARVIS_WHISPER_MODEL": "base"})
    assert chosen.whisper_model == "small"
    assert chosen.whisper_device == "cpu"
