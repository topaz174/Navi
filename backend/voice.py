import logging
import threading

from shared.constants import VOICE_CHUNK, VOICE_MAX_SECONDS, VOICE_SAMPLE_RATE

logger = logging.getLogger(__name__)


class VoiceRecorder:
    def __init__(self):
        self._recording = False
        self._thread: threading.Thread | None = None

    def start(self, on_done, on_error):
        if self._recording:
            return
        self._recording = True
        self._thread = threading.Thread(
            target=self._record, args=(on_done, on_error), daemon=True
        )
        self._thread.start()

    def stop(self):
        self._recording = False

    def _record(self, on_done, on_error):
        try:
            import numpy as np
            import sounddevice as sd
            import speech_recognition as sr
        except ImportError as e:
            self._recording = False
            on_error(f"Voice dependency missing — run: pip install sounddevice SpeechRecognition ({e})")
            return

        try:
            frames = []
            max_chunks = int(VOICE_MAX_SECONDS * VOICE_SAMPLE_RATE / VOICE_CHUNK)
            with sd.InputStream(samplerate=VOICE_SAMPLE_RATE, channels=1, dtype="int16", blocksize=VOICE_CHUNK) as stream:
                while self._recording and len(frames) < max_chunks:
                    data, _ = stream.read(VOICE_CHUNK)
                    frames.append(data.copy())
        except Exception as e:
            self._recording = False
            on_error(f"Microphone error: {e}")
            return

        self._recording = False

        if not frames:
            on_error("No audio captured.")
            return

        audio_np = np.concatenate(frames, axis=0)
        recognizer = sr.Recognizer()
        audio_data = sr.AudioData(audio_np.tobytes(), VOICE_SAMPLE_RATE, 2)

        try:
            text = recognizer.recognize_google(audio_data)
            on_done(text)
        except sr.UnknownValueError:
            on_error("Could not understand audio — please try again.")
        except sr.RequestError as e:
            on_error(f"Transcription service error: {e}")
        except Exception as e:
            on_error(str(e))
