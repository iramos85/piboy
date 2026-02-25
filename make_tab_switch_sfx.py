import math
import os
import wave
import struct

OUT_PATH = "media/ui/tab_switch.wav"
SAMPLE_RATE = 44100

def env_click(t, total):
    # Fast attack, quick decay for a UI "pip" sound
    attack = 0.003
    decay = 0.070
    if t < attack:
        return t / attack
    d = t - attack
    return math.exp(-d / decay)

def tone(freq, t):
    return math.sin(2.0 * math.pi * freq * t)

def make_sound():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

    duration = 0.085  # ~85ms
    n = int(SAMPLE_RATE * duration)
    data = []

    for i in range(n):
        t = i / SAMPLE_RATE

        # Layered retro-ish UI chirp:
        # - main tone around 1450 Hz
        # - a little higher harmonic
        # - tiny downward pitch sweep feel
        sweep = max(0.0, 1.0 - (t / duration))
        f1 = 1450 - (180 * sweep)
        f2 = 2200 - (220 * sweep)

        s = (
            0.70 * tone(f1, t) +
            0.25 * tone(f2, t) +
            0.08 * (1 if math.sin(2 * math.pi * 60 * t) >= 0 else -1)  # tiny grit
        )

        # Envelope + slight soft clip
        s *= env_click(t, duration)
        s = max(-1.0, min(1.0, s))
        s = math.tanh(1.4 * s)

        # Mono 16-bit PCM
        data.append(struct.pack("<h", int(s * 32767)))

    with wave.open(OUT_PATH, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(data))

    print(f"Created: {OUT_PATH}")

if __name__ == "__main__":
    make_sound()