"""Local Whisper transcription. The model is loaded for one clip, then dropped."""

from __future__ import annotations

import gc
import os
import tempfile
import time
from collections.abc import Callable

from jarvis.safety.halt import HaltController
from jarvis.voice.errors import TranscriptionError, VoiceCancelled
from jarvis.voice.gate import VoiceGate

MAX_AUDIO_BYTES = 2_000_000
LOCAL_NOTICE = "Transcribed locally with Whisper small. The recording was not sent to an external service."
WHISPER_MODEL = "small"
BEAM_SIZE = 5


def vad_options():
    from faster_whisper.vad import VadOptions

    return VadOptions(threshold=0.5, min_silence_duration_ms=500, speech_pad_ms=300, max_speech_duration_s=30)


def production_decode_kwargs(probabilities: list[tuple[str, float]] | None = None) -> dict:
    """Settings used for a normal transcription. Diagnostic runs reuse this for profile A.

    The silence filter is off. On a real Hindi/Hinglish recording it cut or spliced speech,
    and the same clip was clearer when that filter was disabled.
    """
    chosen = language_options(list(probabilities or []))
    return {
        "beam_size": BEAM_SIZE,
        "vad_filter": False,
        "condition_on_previous_text": False,
        **chosen,
    }


def comparison_profiles() -> list[dict]:
    """Four decoders for one saved clip. Profile A is the production decoder."""
    return [
        {"id": "A", "label": "Current production", "kind": "production", "vad_filter": True},
        {
            "id": "B",
            "label": "Forced Hindi, no script hint",
            "kind": "fixed",
            "language": "hi",
            "vad_filter": True,
        },
        {
            "id": "C",
            "label": "Automatic language, no script hint",
            "kind": "auto",
            "vad_filter": True,
        },
        {
            "id": "D",
            "label": "Forced Hindi, beam 5, no silence filter",
            "kind": "fixed",
            "language": "hi",
            "vad_filter": False,
        },
    ]


def profile_kwargs(profile: dict, probabilities: list[tuple[str, float]] | None = None) -> dict:
    if profile["kind"] == "production":
        return production_decode_kwargs(probabilities)
    kwargs = {
        "beam_size": BEAM_SIZE,
        "vad_filter": bool(profile["vad_filter"]),
        "condition_on_previous_text": False,
    }
    if profile["vad_filter"]:
        kwargs["vad_parameters"] = vad_options()
    if profile["kind"] == "fixed":
        kwargs["language"] = profile["language"]
    return kwargs


def describe_settings(kwargs: dict) -> dict:
    return {
        "language": kwargs.get("language"),
        "hotwords": kwargs.get("hotwords"),
        "beam_size": kwargs.get("beam_size"),
        "vad_filter": bool(kwargs.get("vad_filter")),
        "condition_on_previous_text": bool(kwargs.get("condition_on_previous_text")),
    }


def trim_leading_spike(samples):
    """Drop a short opening click when quiet follows it and speech starts later.

    On a real English clip that click made Whisper repeat a wrong sentence.
    Speech that is already present at the start is left untouched, and the
    middle of the clip is never cut.
    """
    import numpy as np

    audio = np.asarray(samples)
    rate = 16000
    window = int(0.1 * rate)
    if audio.ndim != 1 or audio.size < rate or window < 1:
        return audio
    loud = []
    for index in range(0, audio.size - window, window):
        chunk = audio[index : index + window]
        loud.append(float(np.sqrt(np.mean(chunk * chunk))) >= 0.02)
    speech_at = None
    for index in range(len(loud) - 1):
        if loud[index] and loud[index + 1]:
            speech_at = index * window
            break
    if speech_at is None or speech_at < window:
        return audio
    cut = speech_at - window // 2
    if cut <= 0 or cut > int(0.45 * rate):
        return audio
    return audio[cut:]


_ENGLISH_WORDS = frozenset(
    {
        "hello",
        "hi",
        "hey",
        "jarvis",
        "my",
        "name",
        "is",
        "please",
        "remember",
        "the",
        "a",
        "an",
        "i",
        "you",
        "me",
        "what",
        "how",
        "when",
        "where",
        "why",
        "this",
        "that",
        "to",
        "of",
        "and",
        "for",
        "it",
        "am",
    }
)
_HINDI_MARKERS = frozenset(
    {"है", "हैं", "था", "थी", "थे", "का", "की", "के", "को", "में", "मैं", "और", "नहीं", "मुझे", "मेरा", "मेरी", "नाम", "बजे"}
)


def _words(text: str) -> list[str]:
    import re

    return re.findall(r"[^\s,.;:!?'\"।]+", text)


def needs_english_retry(probabilities: list[tuple[str, float]]) -> bool:
    """Retry English when Hindi or Urdu won but English still has a real score.

    A third language such as Portuguese stays on one Hindi decode.
    """
    scores = {str(code): float(score) for code, score in probabilities}
    english = scores.get("en", 0.0)
    if not scores or english < 0.12:
        return False
    if language_options(probabilities).get("language") == "en":
        return False
    top = max(scores, key=scores.get)
    return top in {"hi", "ur"}


def english_text_is_clearer(hindi_text: str, english_text: str) -> bool:
    """Use the English decode when the Hindi decode has no Hindi words.

    Indian-accented English can score as Hindi and come back in phonetic
    Devanagari. A Hindi sentence that contains words such as है or बजे stays Hindi.
    """
    if any(word in _HINDI_MARKERS for word in _words(hindi_text)):
        return False
    english_hits = sum(1 for word in _words(english_text) if word.lower() in _ENGLISH_WORDS)
    return english_hits >= 3


def language_options(probabilities: list[tuple[str, float]]) -> dict:
    """Decode as English when English is strictly the top score. Otherwise use Hindi.

    A 0.6 floor sent top-ranked English clips to the Hindi decoder. Third languages
    such as Portuguese stay on Hindi, and a tie with Hindi stays on Hindi.
    """
    scores = {str(code): float(score) for code, score in probabilities}
    english = scores.get("en", 0.0)
    others = [score for code, score in scores.items() if code != "en"]
    if scores and english > max(others, default=-1.0):
        return {"language": "en"}
    return {"language": "hi"}


def inspect_audio(audio: bytes, content_type: str) -> dict:
    """Measure one captured clip. This does not keep the file."""
    summary = {"bytes": len(audio)}
    path = ""
    try:
        import av
        import numpy as np

        install_av_open_compat()
        with tempfile.NamedTemporaryFile(suffix=audio_suffix(content_type), delete=False) as handle:
            handle.write(audio)
            path = handle.name
        with av.open(path, mode="r") as container:
            stream = container.streams.audio[0]
            context = stream.codec_context
            rate = int(context.sample_rate or stream.rate or 0)
            channels = int(context.channels or 0)
            chunks = [frame.to_ndarray() for frame in container.decode(audio=0)]
        summary.update({"codec": context.name, "sample_rate": rate, "channels": channels})
        if not chunks or rate <= 0:
            summary.update({"decoded_seconds": 0, "peak": 0, "rms": 0, "clipping": 0, "silence": 1})
            return summary
        flat = np.concatenate([np.asarray(chunk).reshape(-1) for chunk in chunks])
        if np.issubdtype(flat.dtype, np.integer):
            values = flat.astype(np.float32) / float(np.iinfo(flat.dtype).max)
        else:
            values = flat.astype(np.float32)
        channel_count = max(channels, 1)
        summary.update(
            {
                "decoded_seconds": round(float(values.size) / float(rate * channel_count), 3),
                "peak": round(float(np.max(np.abs(values))), 3),
                "rms": round(float(np.sqrt(np.mean(np.square(values)))), 3),
                "clipping": round(float(np.mean(np.abs(values) >= 0.99)), 3),
                "silence": round(float(np.mean(np.abs(values) < 0.01)), 3),
            }
        )
        return summary
    except Exception as exc:
        summary["error"] = exc.__class__.__name__
        return summary
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


def audio_suffix(content_type: str) -> str:
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    if kind in {"audio/wav", "audio/x-wav"}:
        return ".wav"
    if kind in {"audio/ogg", "audio/opus"}:
        return ".ogg"
    return ".webm"


def transcription_message(exc: BaseException) -> str:
    detail = " ".join(str(exc).split())
    lowered = detail.lower()
    if any(secret in lowered for secret in ("api_key", "authorization", "bearer ", "sk-")):
        detail = exc.__class__.__name__
    if len(detail) > 200:
        detail = detail[:200].rstrip()
    if not detail:
        detail = exc.__class__.__name__
    return f"Speech could not be transcribed: {detail}"


def install_av_open_compat() -> None:
    """PyAV 19 dropped metadata_errors. faster-whisper 1.2.1 still passes it."""
    import av

    current = av.open
    if getattr(current, "_jarvis_compat", False):
        return

    def open_compat(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return current(*args, **kwargs)

    open_compat._jarvis_compat = True
    open_compat._jarvis_original = current
    av.open = open_compat


def cuda_device_count() -> int:
    try:
        import ctranslate2
    except Exception:
        return 0
    try:
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


class FasterModel:
    """One Whisper load. transcribe() consumes the clip and does not keep the file."""

    def __init__(self, model) -> None:
        self._model = model

    def transcribe(self, audio: bytes, suffix: str) -> str:
        path = ""
        install_av_open_compat()
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix or ".webm", delete=False) as handle:
                handle.write(audio)
                path = handle.name
            audio_input = path
            probabilities: list[tuple[str, float]] = []
            if hasattr(self._model, "detect_language"):
                from faster_whisper.audio import decode_audio

                samples = trim_leading_spike(decode_audio(path))
                _language, _probability, probabilities = self._model.detect_language(
                    samples,
                    vad_filter=True,
                    vad_parameters=vad_options(),
                )
                audio_input = samples
            chosen = production_decode_kwargs(probabilities)
            segments, _info = self._model.transcribe(audio_input, **chosen)
            lines = [segment.text for segment in segments]
            if needs_english_retry(probabilities):
                alt = dict(chosen)
                alt["language"] = "en"
                alt_segments, _info = self._model.transcribe(audio_input, **alt)
                alt_lines = [segment.text for segment in alt_segments]
                if english_text_is_clearer("".join(lines), "".join(alt_lines)):
                    lines = alt_lines
            return "".join(lines).strip()
        finally:
            self._model = None
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def compare(self, audio: bytes, suffix: str, halt: HaltController, gate: VoiceGate, generation: int) -> list[dict]:
        """Run profiles A–D on one clip. The caller loads and unloads the model."""
        path = ""
        install_av_open_compat()
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix or ".webm", delete=False) as handle:
                handle.write(audio)
                path = handle.name
            from faster_whisper.audio import decode_audio

            samples = decode_audio(path)
            runs = []
            for profile in comparison_profiles():
                if not gate.current(generation, halt.halted):
                    raise VoiceCancelled("cancelled")
                started = time.perf_counter()
                kwargs = profile_kwargs(profile, [])
                detected_language = ""
                detected_probability = None
                text = ""
                error = ""
                try:
                    detected_language, detected_probability, probabilities = self._detect(
                        samples, bool(profile["vad_filter"])
                    )
                    kwargs = profile_kwargs(profile, probabilities)
                    segments, _info = self._model.transcribe(samples, **kwargs)
                    text = "".join(segment.text for segment in segments).strip()
                except VoiceCancelled:
                    raise
                except Exception as exc:
                    error = transcription_message(exc)
                runs.append(
                    {
                        "id": profile["id"],
                        "label": profile["label"],
                        "text": text,
                        "error": error,
                        "detected_language": detected_language,
                        "detected_probability": detected_probability,
                        "elapsed_s": round(time.perf_counter() - started, 2),
                        "settings": describe_settings(kwargs),
                    }
                )
            return runs
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _detect(self, samples, use_vad: bool) -> tuple[str, float | None, list[tuple[str, float]]]:
        if not hasattr(self._model, "detect_language"):
            return "", None, []
        kwargs: dict = {"vad_filter": use_vad}
        if use_vad:
            kwargs["vad_parameters"] = vad_options()
        language, probability, probabilities = self._model.detect_language(samples, **kwargs)
        return str(language or ""), round(float(probability), 3), list(probabilities or [])

    def close(self) -> None:
        self._model = None


class WhisperTranscriber:
    def __init__(
        self,
        model_factory: Callable[[str, str], FasterModel] | None = None,
        device: str = "auto",
        cuda_probe: Callable[[], bool] | None = None,
    ) -> None:
        self.model_factory = model_factory
        self.device = device if device in {"auto", "cpu", "cuda"} else "auto"
        self.cuda_probe = cuda_probe or (lambda: cuda_device_count() > 0)

    def resolve_device(self) -> tuple[str, str]:
        if self.device == "cpu":
            return "cpu", "int8"
        if self.cuda_probe():
            return "cuda", "float16"
        return "cpu", "int8"

    def transcribe(
        self,
        audio: bytes,
        content_type: str,
        halt: HaltController,
        gate: VoiceGate,
        generation: int,
    ) -> str:
        if not audio:
            raise TranscriptionError("No audio was received.")
        if len(audio) > MAX_AUDIO_BYTES:
            raise TranscriptionError("That recording is too long.")
        if not gate.current(generation, halt.halted):
            raise VoiceCancelled("cancelled")
        if self.model_factory is None:
            raise TranscriptionError("Local transcription is unavailable. Whisper small is not installed.")
        device, compute = self.resolve_device()
        suffix = audio_suffix(content_type)
        try:
            return self._run(audio, suffix, halt, gate, generation, device, compute)
        except VoiceCancelled:
            raise
        except TranscriptionError:
            raise
        except Exception as exc:
            if device != "cuda":
                raise TranscriptionError(transcription_message(exc)) from exc
        try:
            return self._run(audio, suffix, halt, gate, generation, "cpu", "int8")
        except VoiceCancelled:
            raise
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(transcription_message(exc)) from exc

    def _run(
        self,
        audio: bytes,
        suffix: str,
        halt: HaltController,
        gate: VoiceGate,
        generation: int,
        device: str,
        compute: str,
    ) -> str:
        if not gate.current(generation, halt.halted):
            raise VoiceCancelled("cancelled")
        model = self.model_factory(device, compute)
        try:
            if not gate.current(generation, halt.halted):
                raise VoiceCancelled("cancelled")
            text = model.transcribe(audio, suffix)
            if not gate.current(generation, halt.halted):
                raise VoiceCancelled("cancelled")
            return (text or "").strip()
        finally:
            model.close()
            del model
            gc.collect()

    def compare(
        self,
        audio: bytes,
        content_type: str,
        halt: HaltController,
        gate: VoiceGate,
        generation: int,
    ) -> list[dict]:
        if not audio:
            raise TranscriptionError("No audio was received.")
        if len(audio) > MAX_AUDIO_BYTES:
            raise TranscriptionError("That recording is too long.")
        if not gate.current(generation, halt.halted):
            raise VoiceCancelled("cancelled")
        if self.model_factory is None:
            raise TranscriptionError("Local transcription is unavailable. Whisper small is not installed.")
        device, compute = self.resolve_device()
        suffix = audio_suffix(content_type)
        try:
            return self._compare(audio, suffix, halt, gate, generation, device, compute)
        except VoiceCancelled:
            raise
        except TranscriptionError:
            raise
        except Exception as exc:
            if device != "cuda":
                raise TranscriptionError(transcription_message(exc)) from exc
        try:
            return self._compare(audio, suffix, halt, gate, generation, "cpu", "int8")
        except VoiceCancelled:
            raise
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(transcription_message(exc)) from exc

    def _compare(
        self,
        audio: bytes,
        suffix: str,
        halt: HaltController,
        gate: VoiceGate,
        generation: int,
        device: str,
        compute: str,
    ) -> list[dict]:
        if not gate.current(generation, halt.halted):
            raise VoiceCancelled("cancelled")
        model = self.model_factory(device, compute)
        try:
            if not gate.current(generation, halt.halted):
                raise VoiceCancelled("cancelled")
            runs = model.compare(audio, suffix, halt, gate, generation)
            if not gate.current(generation, halt.halted):
                raise VoiceCancelled("cancelled")
            return runs
        finally:
            model.close()
            del model
            gc.collect()


def build_transcriber(download_root, device: str, model_name: str = WHISPER_MODEL) -> WhisperTranscriber:
    def factory(chosen_device: str, compute: str) -> FasterModel:
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise TranscriptionError("Local transcription is unavailable. Whisper small is not installed.") from exc
        download_root.mkdir(parents=True, exist_ok=True)
        model = WhisperModel(
            model_name,
            device=chosen_device,
            compute_type=compute,
            download_root=str(download_root),
            cpu_threads=2,
            num_workers=1,
        )
        return FasterModel(model)

    return WhisperTranscriber(model_factory=factory, device=device)
