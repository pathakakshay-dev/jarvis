# JARVIS

Text assistant for this laptop, through Phase 3. It chats through a replaceable provider, stores conversations and controlled memories in SQLite, accepts push-to-talk speech, can speak a reply, and can be emergency-stopped without asking the model.

Web research, computer control, and later phases are not included.

## Run

Python 3.12 was requested. This PC has Python 3.14.7 and does not have 3.12, so the project virtual environment uses 3.14. The code stays on the 3.12-compatible syntax from the plan. No system Python was installed.

```powershell
C:\Python314\python.exe -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python -m jarvis
```

Open http://127.0.0.1:8765

The default provider is `fake`, which answers locally and does not use an API key. To use Groq or Gemini, set `JARVIS_PROVIDER` and the matching key in `.env`, then start the app again. You can also switch provider from the page while it is running. Model ids must match what that provider currently offers.

Unpaid Gemini requests may be used to improve Google products. The page says so when Gemini is selected.

## Memory

Saved facts are separate from chat and stay in the same SQLite file. JARVIS stores one only when you use the memory panel or an explicit command such as `Remember that I prefer short answers.` A normal reply is not saved. Delete and Clear all ask for confirmation and do nothing without it. Secrets such as passwords and API keys are rejected.

## Voice

Hold **Hold to talk**. The microphone is off until that press and turns off when you release it. The recording is transcribed on this computer with Whisper small. It is not sent to an external service. The transcript appears in the message box. Send it, edit it, or press Cancel. Nothing is sent to chat until you press Send.

**Play** speaks the latest reply. **Stop voice** ends playback and does not replay it. **Mute** is on by default, so new replies stay silent until you uncheck it. Old messages are not spoken when the page loads.

Speaking uses Microsoft Edge TTS and sends the reply text, not the microphone audio, to Microsoft. If that service is unavailable, the browser uses Windows voices and the text stays on this PC. Whisper small loads for one clip and is then dropped. This PC's NVIDIA driver supports CUDA 11.7, and the installed speech library cannot use that GPU, so transcription runs on the CPU in 8-bit mode.

The first transcription downloads Whisper small (`Systran/faster-whisper-small`, about 464 MB) into `data/whisper` if it is not already there. A confident English recording stays on the English decoder. Hindi and Hinglish are not forced onto that decoder.

**Compare decoding** is a separate diagnostic. After you record and listen, press **Keep clip for diagnosis** and agree. That stores one clip under `data/diagnostic` on this computer. **Compare decoding** then runs that same clip through the current decoder and three alternatives, shows each transcript, and deletes the clip. **Delete diagnostic audio** removes it sooner. Emergency Stop and Cancel delete it as well. A leftover clip is deleted the next time the app starts. The comparison does not change the normal Transcribe clip decoder.

## Stop

- Emergency Stop is always on the page. The banner says Stopped only after the app confirms the halt.
- If that confirmation does not arrive, the banner says the stop could not be confirmed. Use End process.
- While the app is running, Ctrl+Alt+Shift+J requests Emergency Stop and Ctrl+Alt+Shift+K ends the process. If a hotkey cannot be registered, the page says so and the buttons still work.
- Resume clears the halt. It does not replay a dropped message, approval, or task.
- Closing the window, or ending Python in Task Manager, is the fallback when the process is frozen.

## Tests

```powershell
.venv\Scripts\python -m pytest
```
