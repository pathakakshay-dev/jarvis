"""Voice failures that the API can report without hiding the cause."""


class VoiceError(Exception):
    pass


class VoiceCancelled(VoiceError):
    pass


class TranscriptionError(VoiceError):
    pass
