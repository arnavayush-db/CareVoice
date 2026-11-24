# CareVoice backend

## Run the API

```bash
cd backend
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-voice.txt
copy .env.example .env   # Windows; use cp on macOS/Linux
```

Set `GOOGLE_API_KEY`, `ELEVENLABS_API_KEY`, and `ELEVENLABS_VOICE_ID` in the local `.env`, then run:

```bash
uvicorn app.main:app --reload --port 8000
```

The FastAPI app creates the SQLite tables and seeds fictional doctors and slots when needed. The React frontend connects to the Pipecat SmallWebRTC endpoints in `app/main.py` for live voice calls.

## Live voice pipeline

`pipecat_worker.py` uses Silero VAD, ElevenLabs realtime STT, Gemini with the availability, booking, and cancellation function tools, and ElevenLabs TTS. The frontend sends and receives audio over WebRTC and receives finalized user/assistant transcript and appointment-update events over the DataChannel. Cancellation requires phone confirmation, appointment selection, and a separate final confirmation.

Keep credentials in `.env`; never commit them. See the root README for frontend setup, API routes, and demo limitations.
