
import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity, AudioLines, CalendarDays, Check, ChevronDown,
  Clock3, HeartPulse, Mic, MicOff, Phone, PhoneOff,
  Search, ShieldCheck, Stethoscope, UserRound, Users,
  X, Plus, ArrowUpRight
} from 'lucide-react';
import './styles.css';

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const money = n => `₹${Number(n).toLocaleString('en-IN')}`;

function App() {
  const [doctors, setDoctors] = useState([]);
  const [slots, setSlots] = useState([]);
  const [appointments, setAppointments] = useState([]);

  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState('');
  const [selectedSlot, setSelectedSlot] = useState('');
  const [patient, setPatient] = useState('');
  const [phone, setPhone] = useState('');
  const [confirmed, setConfirmed] = useState(false);

  const [cancelPhone, setCancelPhone] = useState('');
  const [cancelPhoneConfirmed, setCancelPhoneConfirmed] = useState(false);
  const [cancelFinalConfirmed, setCancelFinalConfirmed] = useState(false);
  const [cancelAppointments, setCancelAppointments] = useState([]);
  const [selectedCancelAppointment, setSelectedCancelAppointment] = useState('');
  const [cancelSearching, setCancelSearching] = useState(false);

  const [messages, setMessages] = useState([
    {
      role: 'assistant',
      text: 'Good evening. I’m CareVoice, your clinic assistant. I can help you find a doctor, book a visit, or manage an existing appointment.'
    }
  ]);

  const [input, setInput] = useState('');
  const [callOpen, setCallOpen] = useState(false);
  const [listening, setListening] = useState(false);
  const [callStatus, setCallStatus] = useState('Disconnected');
  const [transcriptChannelOpen, setTranscriptChannelOpen] = useState(false);
  const [callSeconds, setCallSeconds] = useState(0);
  const [notice, setNotice] = useState('');
  const [activePage, setActivePage] = useState('overview');

  const peerConnection = useRef(null);
  const transcriptChannel = useRef(null);
  const microphoneStream = useRef(null);
  const remoteAudio = useRef(null);
  const callGeneration = useRef(0);
  const chatEnd = useRef(null);

  // Conversation ID: same ID throughout one call.
  const conversationId = useRef(null);

  const newConversationId = () => {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }

    return `carevoice-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  };

  const getConversationId = () => {
    if (!conversationId.current) {
      conversationId.current = newConversationId();
    }

    return conversationId.current;
  };

  // LOAD DASHBOARD DATA

  const refresh = async () => {
    try {
      const [d, s, a] = await Promise.all([
        fetch(`${API}/api/doctors`),
        fetch(`${API}/api/slots`),
        fetch(`${API}/api/appointments`)
      ]);

      if (!d.ok || !s.ok || !a.ok) {
        throw new Error('API request failed');
      }

      setDoctors(await d.json());
      setSlots(await s.json());
      setAppointments(await a.json());

    } catch {
      setNotice('Cannot reach API. Start the backend on port 8000.');

    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  // AUTO SCROLL CHAT

  useEffect(() => {
    chatEnd.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // CALL TIMER

  useEffect(() => {
    if (!callOpen) return;

    const timer = setInterval(() => {
      setCallSeconds(s => s + 1);
    }, 1000);

    return () => clearInterval(timer);
  }, [callOpen]);

  // NAVIGATION

  const navigate = (page, target) => {
    setActivePage(page);

    setTimeout(() => {
      document.getElementById(target)?.scrollIntoView({
        behavior: 'smooth',
        block: 'start'
      });
    }, 50);
  };

  // CHAT MESSAGE

  const addMessage = (role, text) => {
    setMessages(m => [
      ...m,
      {
        role,
        text,
        time: new Date().toLocaleTimeString([], {
          hour: '2-digit',
          minute: '2-digit'
        })
      }
    ]);
  };

  // TEXT TO SPEECH

  const speak = text => {
    if ('speechSynthesis' in window) {
      speechSynthesis.cancel();

      const utterance = new SpeechSynthesisUtterance(text);
      utterance.rate = 1;

      speechSynthesis.speak(utterance);
    }
  };

  // SEND MESSAGE TO BACKEND

  const send = async (text = input) => {
    if (!text.trim()) return;

    addMessage('user', text);
    setInput('');

    try {
      const response = await fetch(`${API}/api/assistant`, {
        method: 'POST',

        headers: {
          'Content-Type': 'application/json'
        },

        body: JSON.stringify({
          message: text,
          phone: phone || cancelPhone,
          patient_name: patient,
          conversation_id: getConversationId()
        })
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(
          data.detail || 'Assistant request failed'
        );
      }

      addMessage('assistant', data.reply);
      if (!callOpen) speak(data.reply);

      if (data.slots?.length) {
        setSlots(data.slots);
      }

      // Refresh dashboard after assistant action.
      await refresh();

    } catch (error) {
      console.error(error);

      addMessage(
        'assistant',
        'I can’t connect to the clinic server right now. Please check that the backend is running.'
      );
    }
  };

  // START VOICE CALL

  const closeVoiceConnection = (reason = 'call cleanup') => {
    console.info(`Closing CareVoice WebRTC session: ${reason}`);
    callGeneration.current += 1;

    if (peerConnection.current) {
      peerConnection.current.onconnectionstatechange = null;
      peerConnection.current.ontrack = null;
      transcriptChannel.current?.close();
      transcriptChannel.current = null;
      peerConnection.current.close();
      peerConnection.current = null;
    }

    microphoneStream.current?.getTracks().forEach(track => track.stop());
    microphoneStream.current = null;

    if (remoteAudio.current) {
      remoteAudio.current.pause();
      remoteAudio.current.srcObject = null;
    }

    setListening(false);
    setTranscriptChannelOpen(false);
  };

  // START VOICE CALL

  const startCall = async () => {
    if (peerConnection.current) return;

    // Every new call gets a fresh conversation ID.
    conversationId.current = newConversationId();
    const generation = ++callGeneration.current;

    setCallOpen(true);
    setCallSeconds(0);
    setCallStatus('Requesting microphone…');
    setTranscriptChannelOpen(false);
    let callStage = 'microphone permission';

    try {
      console.info('CareVoice voice call: requesting microphone access');
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

      if (generation !== callGeneration.current) {
        stream.getTracks().forEach(track => track.stop());
        return;
      }

      microphoneStream.current = stream;
      console.info('CareVoice microphone acquired:', {
        audioTracks: stream.getAudioTracks().length,
        trackStates: stream.getAudioTracks().map(track => track.readyState)
      });
      callStage = 'peer connection creation';
      console.info('CareVoice voice call: creating RTCPeerConnection');
      const connection = new RTCPeerConnection();
      peerConnection.current = connection;

      // This must be created before createOffer so the SCTP m-line is in the SDP.
      const channel = connection.createDataChannel('carevoice-transcript');
      transcriptChannel.current = channel;
      console.info('CareVoice transcript DataChannel created:', channel.readyState);
      channel.onopen = () => {
        if (transcriptChannel.current === channel) {
          console.info('CareVoice transcript DataChannel opened');
          setTranscriptChannelOpen(true);
        }
      };
      channel.onmessage = event => {
        try {
          if (typeof event.data !== 'string') return;
          const message = JSON.parse(event.data);
          if (
            message.type === 'transcript' &&
            ['user', 'assistant'].includes(message.role) &&
            typeof message.text === 'string' &&
            message.text.trim()
          ) {
            console.debug('CareVoice transcript received:', message.role, message.text);
            addMessage(message.role, message.text.trim());
          } else if (
            message.type === 'appointment_update' &&
            ['booked', 'cancelled'].includes(message.action)
          ) {
            console.info('CareVoice appointment changed during voice call:', message.action);
            void refresh();
          }
        } catch (error) {
          console.error('Could not read a Pipecat transcript event:', error, event.data);
        }
      };
      channel.onerror = event => {
        console.error('CareVoice transcript DataChannel error:', event);
        if (transcriptChannel.current === channel) {
          setNotice('The voice call is connected, but live transcript updates are unavailable.');
        }
      };
      channel.onclose = () => {
        console.warn('CareVoice transcript DataChannel closed');
        if (transcriptChannel.current === channel) {
          transcriptChannel.current = null;
          setTranscriptChannelOpen(false);
          setNotice('The live transcript channel closed. End and restart the call to restore it.');
        }
      };

      stream.getAudioTracks().forEach(track => connection.addTrack(track, stream));

      connection.ontrack = async event => {
        console.info('CareVoice received remote media track:', event.track.kind);
        const audio = remoteAudio.current;
        if (!audio) return;

        audio.srcObject = event.streams[0] || new MediaStream([event.track]);
        try {
          await audio.play();
        } catch (error) {
          console.error('Remote audio playback could not start:', error);
          setCallStatus('Tap the call window to enable audio');
        }
      };

      connection.onconnectionstatechange = () => {
        console.info('CareVoice WebRTC connection state:', connection.connectionState);
        if (connection.connectionState === 'connected') {
          setCallStatus('Connected');
          setListening(connection.getSenders().some(
            sender => sender.track?.kind === 'audio' && sender.track.enabled
          ));
        } else if (connection.connectionState === 'disconnected') {
          setCallStatus('Reconnecting…');
        } else if (connection.connectionState === 'failed') {
          console.error('CareVoice peer connection failed:', {
            connectionState: connection.connectionState,
            iceConnectionState: connection.iceConnectionState,
            signalingState: connection.signalingState
          });
          closeVoiceConnection('peer connection entered failed state');
          setCallOpen(false);
          setCallStatus('Connection failed');
          setNotice(
            `WebRTC connection failed (ICE: ${connection.iceConnectionState}, signaling: ${connection.signalingState}). Check the browser console and backend logs for details.`
          );
        }
      };
      connection.oniceconnectionstatechange = () => {
        console.info('CareVoice ICE connection state:', connection.iceConnectionState);
      };
      connection.onicegatheringstatechange = () => {
        console.info('CareVoice ICE gathering state:', connection.iceGatheringState);
      };
      connection.onsignalingstatechange = () => {
        console.info('CareVoice signaling state:', connection.signalingState);
      };
      connection.onicecandidateerror = event => {
        console.error('CareVoice ICE candidate error:', {
          url: event.url,
          errorCode: event.errorCode,
          errorText: event.errorText
        });
      };

      callStage = 'local SDP offer creation';
      const offer = await connection.createOffer();
      callStage = 'setting local SDP offer';
      await connection.setLocalDescription(offer);
      const localSdp = connection.localDescription?.sdp || '';
      console.info('CareVoice local SDP created:', {
        type: connection.localDescription?.type,
        hasAudioSection: /^m=audio(?: |$)/m.test(localSdp),
        hasDataChannelSection: /^m=application(?: |$)/m.test(localSdp)
      });

      // Pipecat 1.17 accepts gathered candidates in the initial SDP offer.
      if (connection.iceGatheringState !== 'complete') {
        callStage = 'ICE gathering';
        setCallStatus('Gathering network candidates…');
        console.info('CareVoice ICE gathering started');
        await new Promise((resolve, reject) => {
          const timeout = setTimeout(() => {
            connection.removeEventListener('icegatheringstatechange', onGathering);
            reject(new Error('Timed out while gathering network candidates.'));
          }, 15000);

          const onGathering = () => {
            if (connection.iceGatheringState === 'complete') {
              clearTimeout(timeout);
              connection.removeEventListener('icegatheringstatechange', onGathering);
              resolve();
            }
          };

          connection.addEventListener('icegatheringstatechange', onGathering);
          onGathering();
        });
      }

      const gatheredSdp = connection.localDescription?.sdp || '';
      console.info('CareVoice local ICE gathering complete:', {
        state: connection.iceGatheringState,
        iceCandidateCount: (gatheredSdp.match(/^a=candidate:/gm) || []).length
      });

      if (generation !== callGeneration.current) return;

      callStage = 'sending SDP offer to the backend';
      setCallStatus('Connecting to voice assistant…');
      console.info('CareVoice posting WebRTC SDP offer to', `${API}/api/offer`);
      const response = await fetch(`${API}/api/offer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sdp: connection.localDescription.sdp,
          type: connection.localDescription.type
        })
      });
      console.info('CareVoice /api/offer HTTP status:', response.status);
      const responseBody = await response.text();
      let answer;
      try {
        answer = JSON.parse(responseBody);
      } catch (error) {
        throw new Error(
          `Backend returned non-JSON response (HTTP ${response.status}): ${responseBody.slice(0, 200)}`
        );
      }

      if (!response.ok) {
        throw new Error(answer.detail || 'The Pipecat offer request failed.');
      }
      if (!answer.sdp || !answer.type || !answer.pc_id) {
        throw new Error('The Pipecat server returned an incomplete SDP answer.');
      }

      if (generation !== callGeneration.current) return;
      callStage = 'applying backend SDP answer';
      console.info('CareVoice backend SDP answer:', {
        peerId: answer.pc_id,
        type: answer.type,
        hasAudioSection: /^m=audio(?: |$)/m.test(answer.sdp),
        hasDataChannelSection: /^m=application(?: |$)/m.test(answer.sdp),
        iceCandidateCount: (answer.sdp.match(/^a=candidate:/gm) || []).length
      });
      await connection.setRemoteDescription(answer);
      if (generation !== callGeneration.current) return;
      console.info('CareVoice remote SDP answer applied');
      setCallStatus(
        connection.connectionState === 'connected' ? 'Connected' : 'Connecting audio…'
      );
    } catch (error) {
      console.error(`Pipecat WebRTC call failed during ${callStage}:`, error);
      if (generation === callGeneration.current) {
        closeVoiceConnection(`setup failed during ${callStage}`);
        setCallOpen(false);
        setCallStatus('Connection failed');
        setNotice(
          error.name === 'NotAllowedError'
            ? 'Microphone access is required to start a voice call.'
            : `Call failed during ${callStage}: ${error.message || 'unknown error'}`
        );
      }
    }
  };

  // END VOICE CALL

  const endCall = () => {
    closeVoiceConnection('user ended call');
    setCallOpen(false);
    setCallStatus('Disconnected');
  };

  // MICROPHONE

  const toggleMic = () => {
    const audioTrack = microphoneStream.current?.getAudioTracks()[0];
    if (!audioTrack || !peerConnection.current) {
      setNotice('The voice connection is not ready yet.');
      return;
    }

    audioTrack.enabled = !audioTrack.enabled;
    setListening(audioTrack.enabled);
  };

  // MANUAL BOOKING

  const bookSelected = async () => {
    if (
      !selectedSlot ||
      !patient.trim() ||
      !phone.trim() ||
      !confirmed
    ) {
      setNotice(
        'Select a slot, enter the patient name and phone number, then confirm the number.'
      );
      return;
    }

    try {
      const response = await fetch(`${API}/api/appointments`, {
        method: 'POST',

        headers: {
          'Content-Type': 'application/json'
        },

        body: JSON.stringify({
          slot_id: Number(selectedSlot),
          patient_name: patient,
          phone,
          confirm_phone: confirmed
        })
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || 'Booking failed');
      }

      setNotice(
        `Appointment confirmed with ${data.doctor} on ${data.date} at ${data.time}.`
      );

      addMessage(
        'assistant',
        `Your appointment is confirmed with ${data.doctor} on ${data.date} at ${data.time}.`
      );

      speak('Your appointment is confirmed.');

      setSelectedSlot('');
      setConfirmed(false);

      await refresh();

    } catch (error) {
      setNotice(error.message);
    }
  };

  // MANUAL CANCELLATION LOOKUP

  const lookupCancellation = async () => {
    if (!cancelPhone.trim() || !cancelPhoneConfirmed) {
      setNotice('Read the registered phone number back and confirm it first.');
      return;
    }

    setCancelSearching(true);
    setCancelAppointments([]);
    setSelectedCancelAppointment('');
    setCancelFinalConfirmed(false);

    try {
      const response = await fetch(`${API}/api/appointments/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          phone: cancelPhone,
          confirm_phone: true
        })
      });
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || 'Could not look up appointments.');
      }

      const matches = Array.isArray(data.appointments) ? data.appointments : [];
      setCancelAppointments(matches);
      setNotice(
        matches.length
          ? `Found ${matches.length} active appointment${matches.length === 1 ? '' : 's'}. Select one, then confirm cancellation separately.`
          : 'No active appointments were found for that phone number.'
      );
    } catch (error) {
      setNotice(error.message);
    } finally {
      setCancelSearching(false);
    }
  };

  const cancel = async () => {
    if (!cancelPhone.trim() || !cancelPhoneConfirmed) {
      setNotice('Read the registered phone number back and confirm it first.');
      return;
    }

    if (!selectedCancelAppointment) {
      setNotice('Select the appointment you want to cancel.');
      return;
    }

    if (!cancelFinalConfirmed) {
      setNotice('Confirm cancellation of the selected appointment first.');
      return;
    }

    try {
      const response = await fetch(`${API}/api/appointments/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          phone: cancelPhone,
          confirm_phone: true,
          appointment_id: Number(selectedCancelAppointment),
          confirm_cancellation: true
        })
      });
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || 'Cancellation failed.');
      }

      const appointment = data.appointment;
      setNotice(
        `Cancelled the appointment with ${appointment.doctor} on ${appointment.date} at ${appointment.time}.`
      );
      setCancelAppointments([]);
      setSelectedCancelAppointment('');
      setCancelPhoneConfirmed(false);
      setCancelFinalConfirmed(false);
      setCancelPhone('');
      await refresh();
    } catch (error) {
      setNotice(error.message);
    }
  };

  // SEARCH DOCTORS

  const filtered = doctors.filter(d =>
    (d.name + d.specialty)
      .toLowerCase()
      .includes(query.toLowerCase())
  );

  // TIMER FORMAT

  const fmtTime = n =>
    `${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}`;

  return (
    <div className="app-shell">

      {/* SIDEBAR */}

      <aside className="sidebar">

        <div className="brand">
          <div className="brand-icon">
            <HeartPulse size={21} />
          </div>

          <div>
            <b>carevoice</b>
            <small>CLINIC OPERATIONS</small>
          </div>
        </div>

        <div className="workspace">
          <span className="clinic-dot" />
          <span>Riverside Medical</span>
          <ChevronDown size={14} />
        </div>

        <div className="nav-label">WORKSPACE</div>

        <button
          className={`nav ${activePage === 'overview' ? 'active' : ''}`}
          onClick={() => navigate('overview', 'overview')}
        >
          <Activity size={17} />
          Overview
        </button>

        <button
          className={`nav ${activePage === 'appointments' ? 'active' : ''}`}
          onClick={() => navigate('appointments', 'appointments')}
        >
          <CalendarDays size={17} />
          Appointments

          <span className="nav-count">
            {appointments.filter(a => a.status === 'booked').length}
          </span>
        </button>

        <button
          className={`nav ${activePage === 'doctors' ? 'active' : ''}`}
          onClick={() => navigate('doctors', 'doctors')}
        >
          <Stethoscope size={17} />
          Doctors
        </button>

        <div className="sidebar-bottom">

          <div className="secure">
            <ShieldCheck size={17} />

            <div>
              <b>Private & secure</b>
              <small>Demo environment</small>
            </div>
          </div>

          <div className="profile">
            <div className="avatar">AA</div>

            <div>
              <b>Clinic Admin</b>
              <small>Administrator</small>
            </div>

            <ChevronDown size={15} />
          </div>

        </div>
      </aside>

      {/* MAIN DASHBOARD */}

      <main className="main">

        <header className="topbar">

          <div className="crumb">
            Workspace <span>/</span> <b>{activePage}</b>
          </div>

          <div className="top-actions">
            <span className="live-status">
              <i /> SYSTEM OPERATIONAL
            </span>

            <div className="top-avatar">AA</div>
          </div>

        </header>

        <div className="content">

          {/* PAGE HEADING */}

          <div id="overview" className="page-heading">

            <div>
              <div className="eyebrow">
                MONDAY, 23 NOVEMBER 2026
              </div>

              <h1>
                Good evening, Admin <span>✦</span>
              </h1>

              <p>
                Here’s what’s happening at your clinic today.
              </p>
            </div>

            <button className="primary" onClick={startCall}>
              <Phone size={16} />
              Try a voice call
            </button>

          </div>

          {/* NOTICE */}

          {notice && (
            <div className="notice">
              <span>{notice}</span>

              <button onClick={() => setNotice('')}>
                <X size={15} />
              </button>
            </div>
          )}

          {/* STATISTICS */}

          <section className="stats">

            <div className="stat-card">
              <div className="stat-top">
                <span>Active appointments</span>
                <CalendarDays size={17} />
              </div>

              <strong>
                {appointments.filter(a => a.status === 'booked').length}
              </strong>

              <small>Across all doctors</small>
            </div>

            <div className="stat-card">
              <div className="stat-top">
                <span>Available slots</span>
                <Clock3 size={17} />
              </div>

              <strong>{slots.length}</strong>
              <small>Ready to be booked</small>
            </div>

            <div className="stat-card">
              <div className="stat-top">
                <span>Medical professionals</span>
                <Users size={17} />
              </div>

              <strong>{doctors.length}</strong>
              <small>Across specialties</small>
            </div>

            <div className="stat-card accent-stat">
              <div className="stat-top">
                <span>Voice assistant</span>
                <AudioLines size={17} />
              </div>

              <strong className="online-text">Online</strong>
              <small><i /> Ready to take calls</small>
            </div>

          </section>

          {/* DOCTORS AND VOICE ASSISTANT */}

          <div className="dashboard-grid">

            <section className="panel doctors-panel" id="doctors">

              <div className="panel-heading">

                <div>
                  <h2>Our doctors</h2>
                  <p>Browse specialists and open appointment slots</p>
                </div>

                <button
                  className="text-button"
                  onClick={() => navigate('doctors', 'doctors')}
                >
                  View all <ArrowUpRight size={14} />
                </button>

              </div>

              <div className="searchbox">
                <Search size={16} />

                <input
                  placeholder="Search doctors or specialties..."
                  value={query}
                  onChange={e => setQuery(e.target.value)}
                />
              </div>

              <div className="doctor-list">

                {filtered.map((d, i) => (
                  <div className="doctor-row" key={d.id}>

                    <div className={`doctor-avatar tone-${i % 4}`}>
                      <Stethoscope size={19} />
                    </div>

                    <div className="doctor-info">
                      <b>{d.name}</b>
                      <span>{d.specialty} · {d.experience}</span>
                    </div>

                    <div className="doctor-fee">
                      <b>{money(d.fee)}</b>
                      <span>Consultation</span>
                    </div>

                    <button
                      className="icon-button"
                      title="Select doctor"
                      onClick={() => {
                        const slot = slots.find(
                          s => s.doctor_id === d.id
                        );

                        setSelectedSlot(String(slot?.id || ''));
                        navigate('overview', 'booking');
                      }}
                    >
                      <ArrowUpRight size={17} />
                    </button>

                  </div>
                ))}

              </div>
            </section>

            {/* VOICE ASSISTANT CARD */}

            <section className="panel call-panel">

              <div className="panel-heading">

                <div>
                  <h2>Talk to CareVoice</h2>
                  <p>Your AI-powered appointment assistant</p>
                </div>

                <span className="pulse-dot" />

              </div>

              <div className="assistant-art">

                <div className="orbit orbit-one" />
                <div className="orbit orbit-two" />

                <div className="assistant-core">
                  <AudioLines size={34} />
                </div>

                <span className="spark s1">✦</span>
                <span className="spark s2">✧</span>

              </div>

              <div className="call-copy">
                <b>Appointments, made effortless.</b>

                <span>
                  Ask about availability, book a visit, or cancel an existing
                  appointment — all by voice.
                </span>
              </div>

              <button className="call-cta" onClick={startCall}>
                <Phone size={16} />
                Start a call
                <ArrowUpRight size={15} />
              </button>

              <div className="call-foot">
                <ShieldCheck size={14} />
                No account or OTP required for this demo
              </div>

            </section>

          </div>

          {/* APPOINTMENTS TABLE */}

          <div className="lower-grid">

            <section className="panel" id="appointments">

              <div className="panel-heading">

                <div>
                  <h2>Upcoming appointments</h2>
                  <p>Latest bookings and cancellations</p>
                </div>

                <span className="count-pill">
                  {appointments.length} total
                </span>

              </div>

              <div className="table-wrap">

                <table>

                  <thead>
                    <tr>
                      <th>PATIENT</th>
                      <th>DOCTOR</th>
                      <th>DATE & TIME</th>
                      <th>STATUS</th>
                    </tr>
                  </thead>

                  <tbody>

                    {appointments.slice(0, 6).map(a => (
                      <tr key={a.id}>

                        <td>
                          <div className="patient-cell">
                            <div className="mini-avatar">
                              <UserRound size={14} />
                            </div>

                            <b>{a.patient_name}</b>
                          </div>
                        </td>

                        <td>{a.doctor}</td>

                        <td>
                          <b>{a.date}</b>
                          <small>{a.time}</small>
                        </td>

                        <td>
                          <span className={`status ${a.status}`}>
                            {a.status}
                          </span>
                        </td>

                      </tr>
                    ))}

                    {!appointments.length && (
                      <tr>
                        <td colSpan="4" className="empty">
                          No appointments yet. Book one to see it here.
                        </td>
                      </tr>
                    )}

                  </tbody>
                </table>

              </div>
            </section>

            {/* QUICK ACTIONS */}

            <section className="panel quick-panel">

              <div className="panel-heading">
                <div>
                  <h2>Quick actions</h2>
                  <p>Manage appointments directly</p>
                </div>
              </div>

              <div className="quick-action">

                <div className="quick-icon">
                  <Plus size={17} />
                </div>

                <div>
                  <b>Book an appointment</b>
                  <span>Choose a doctor and available time</span>
                </div>

                <button
                  onClick={() => navigate('overview', 'booking')}
                >
                  <ArrowUpRight size={16} />
                </button>

              </div>

              <div className="quick-action">

                <div className="quick-icon cancel-icon">
                  <X size={17} />
                </div>

                <div>
                  <b>Cancel an appointment</b>
                  <span>Find a booking using phone number</span>
                </div>

                <button
                  onClick={() => navigate('overview', 'cancellation')}
                >
                  <ArrowUpRight size={16} />
                </button>

              </div>

            </section>

          </div>

          {/* BOOKING AND CANCELLATION FORMS */}

          <div className="lower-grid forms-grid">

            {/* BOOKING FORM */}

            <section className="panel" id="booking">

              <div className="panel-heading">
                <div>
                  <h2>Book a visit</h2>
                  <p>Phone number confirmation is required</p>
                </div>
              </div>

              <div className="form-grid">

                <label className="field wide">
                  Available slot

                  <select
                    value={selectedSlot}
                    onChange={e => setSelectedSlot(e.target.value)}
                  >
                    <option value="">
                      Choose a doctor, date and time
                    </option>

                    {slots.map(s => (
                      <option key={s.id} value={s.id}>
                        {s.doctor} · {s.date} · {s.time}
                      </option>
                    ))}

                  </select>
                </label>

                <label className="field">
                  Patient name

                  <input
                    value={patient}
                    onChange={e => setPatient(e.target.value)}
                    placeholder="Full name"
                  />
                </label>

                <label className="field">
                  Phone number

                  <input
                    value={phone}
                    onChange={e => {
                      setPhone(e.target.value);
                      setConfirmed(false);
                    }}
                    placeholder="Number spoken by caller"
                  />
                </label>

              </div>

              <label className="checkline">
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={e => setConfirmed(e.target.checked)}
                />

                I have read the phone number back and the caller confirmed it.
              </label>

              <button
                className="primary form-submit"
                onClick={bookSelected}
              >
                Confirm booking <Check size={15} />
              </button>

            </section>

            {/* CANCELLATION FORM */}

            <section className="panel" id="cancellation">

              <div className="panel-heading">
                <div>
                  <h2>Cancel a visit</h2>
                  <p>Appointments are looked up by phone number</p>
                </div>
              </div>

              <label className="field">
                Patient phone number

                <input
                  value={cancelPhone}
                  onChange={e => {
                    setCancelPhone(e.target.value);
                    setCancelPhoneConfirmed(false);
                    setCancelFinalConfirmed(false);
                    setCancelAppointments([]);
                    setSelectedCancelAppointment('');
                  }}
                  placeholder="Enter the registered number"
                />
              </label>

              <label className="checkline">
                <input
                  type="checkbox"
                  checked={cancelPhoneConfirmed}
                  onChange={e => setCancelPhoneConfirmed(e.target.checked)}
                />

                I read the number back and the caller confirmed it.
              </label>

              <button
                className="primary form-submit"
                onClick={lookupCancellation}
                disabled={cancelSearching}
              >
                {cancelSearching ? 'Finding appointments…' : 'Find appointments'} <Search size={15} />
              </button>

              {cancelAppointments.length > 0 && (
                <>
                  <label className="field">
                    Select the appointment
                    <select
                      value={selectedCancelAppointment}
                      onChange={e => {
                        setSelectedCancelAppointment(e.target.value);
                        setCancelFinalConfirmed(false);
                      }}
                    >
                      <option value="">Choose an appointment</option>
                      {cancelAppointments.map(appointment => (
                        <option key={appointment.id} value={appointment.id}>
                          {appointment.doctor} · {appointment.date} · {appointment.time}
                        </option>
                      ))}
                    </select>
                  </label>

                  {selectedCancelAppointment && (
                    <>
                      <label className="checkline">
                        <input
                          type="checkbox"
                          checked={cancelFinalConfirmed}
                          onChange={e => setCancelFinalConfirmed(e.target.checked)}
                        />
                        I confirm cancellation of the selected appointment.
                      </label>

                      <button
                        className="secondary form-submit"
                        onClick={cancel}
                      >
                        Confirm cancellation <X size={15} />
                      </button>
                    </>
                  )}
                </>
              )}

            </section>

          </div>

          <footer>
            CAREVOICE <span>·</span> Demo environment with fictional patient and doctor data
          </footer>

        </div>
      </main>

      {/* VOICE CALL WINDOW */}

      {callOpen && (

        <div className="call-overlay">

          <div className="phone-window">

            <audio ref={remoteAudio} autoPlay playsInline style={{ display: 'none' }} />

            <div className="phone-top">
              <span><i /> {callStatus.toUpperCase()}</span>

              <button onClick={endCall}>
                <X size={18} />
              </button>
            </div>

            <div className="phone-person">

              <div className="phone-avatar">
                <AudioLines size={32} />
              </div>

              <h2>CareVoice</h2>
              <p>AI Appointment Assistant</p>

              <div className="timer">
                {fmtTime(callSeconds)}
              </div>

              <div className="wave-bars">
                {Array.from({ length: 28 }, (_, i) => (
                  <i
                    key={i}
                    style={{
                      height: `${10 + Math.abs(Math.sin(i * 1.8)) * 32}px`
                    }}
                  />
                ))}
              </div>

            </div>

            {/* LIVE TRANSCRIPT */}

            <div className="transcript">

              <div className="transcript-title">
                LIVE TRANSCRIPT <span>● UPDATING</span>
              </div>

              {messages.slice(-5).map((m, i) => (
                <div
                  className={`transcript-line ${m.role}`}
                  key={i}
                >
                  <b>
                    {m.role === 'user' ? 'YOU' : 'CAREVOICE'}
                  </b>

                  <span>{m.text}</span>
                </div>
              ))}

              <div ref={chatEnd} />

            </div>

            {/* CALL CONTROLS */}

            <div className="call-controls">

              <button
                className={`mic-button ${listening ? 'listening' : ''}`}
                onClick={toggleMic}
              >
                {listening
                  ? <MicOff size={20} />
                  : <Mic size={20} />
                }
              </button>

              <button
                className="end-button"
                onClick={endCall}
              >
                <PhoneOff size={20} />
              </button>

            </div>

            <div className="call-hint">
              {callStatus === 'Connected'
                ? !transcriptChannelOpen
                  ? 'Voice connected · opening transcript channel…'
                  : listening
                    ? 'Microphone on · speak to the assistant'
                    : 'Microphone muted'
                : callStatus}
            </div>

            {/* TEXT INPUT */}

            <div className="phone-chat">

              <input
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && send()}
                placeholder="Or type a message..."
              />

              <button onClick={() => send()}>
                <ArrowUpRight size={18} />
              </button>

            </div>

          </div>
        </div>
      )}

    </div>
  );
}

createRoot(document.getElementById('root')).render(<App />);
