# CareVoice — Voice AI Doctor Appointment Assistant

CareVoice is a React clinic dashboard with a FastAPI and SQLite backend. Its browser voice-call button uses Pipecat SmallWebRTC: microphone audio is sent to the backend, where Silero VAD, ElevenLabs realtime STT, Gemini with appointment function tools, and ElevenLabs TTS form the voice pipeline. Finalized user and assistant transcripts are relayed to the call window over the WebRTC DataChannel.

The voice path uses WebRTC audio and does not use browser `SpeechRecognition` or `speechSynthesis` as a call fallback. The separate text chat endpoint is `/api/assistant`.

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer
- A browser with WebRTC and microphone support
- Google Gemini and ElevenLabs credentials for live voice calls

## Run locally

### Backend

```bash
cd backend
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-voice.txt
copy .env.example .env
```

Add `GOOGLE_API_KEY`, `ELEVENLABS_API_KEY`, and `ELEVENLABS_VOICE_ID` to `backend/.env` locally. Keep the real credentials out of source control. Then start the API:

```bash
uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. The API documentation is available at http://localhost:8000/docs. Set `VITE_API_URL` only when the backend is hosted at a different URL.

## Voice pipeline and appointment tools

`backend/app/main.py` exposes the SmallWebRTC signaling endpoints `POST /api/offer` and `PATCH /api/offer`. The frontend creates a peer connection, sends microphone audio, plays remote audio, and receives transcript and appointment-update events over its DataChannel.

`backend/pipecat_worker.py` builds the Pipecat pipeline:

1. Browser audio enters Silero VAD and ElevenLabs realtime STT.
2. Gemini receives the recognized user turn and can call the registered `check_doctor_availability`, `book_appointment`, and `cancel_appointment` tools.
3. Assistant text is sent to ElevenLabs TTS and returned as WebRTC audio.
4. Finalized user and assistant transcripts are relayed to the call window.

Booking requires the phone number to be explicitly confirmed. Cancellation looks up appointments only after phone confirmation, requires the caller to select an appointment, and requires a separate cancellation confirmation before changing its status. Dashboard cancellation follows the same lookup, selection, and confirmation steps.

The dashboard loads doctors, future slots, and appointments from the backend. Appointment records and seeded fictional doctors/slots are stored in SQLite. Voice booking and cancellation emit dashboard refresh events.

## API overview

- `GET /api/health`
- `GET /api/doctors`
- `GET /api/slots?doctor_id=1&date=YYYY-MM-DD`
- `GET /api/appointments`
- `POST /api/appointments`
- `POST /api/appointments/cancel`
- `POST /api/assistant` (text chat)
- `POST /api/offer` and `PATCH /api/offer` (SmallWebRTC signaling)

## Configuration and limitations

`backend/.env.example` contains empty provider-key placeholders and local development settings. Copy it to `.env` and fill in credentials locally; do not commit `.env`.

The records are fictional and the database is local demo data. This project is not a real medical booking service. A live call depends on valid provider credentials, microphone permission, and a network path that permits WebRTC. Verify provider connectivity and complete an end-to-end call before presenting a deployment as operational.
