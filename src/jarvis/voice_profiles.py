"""Fixed, inspectable Chatterbox auditions. No caller-supplied paths or model code."""

MODEL = "Chatterbox Multilingual V3"
CHECKPOINT = "5de7a54aa4e5e2baadb0182dde554908b48b85c2"
REFERENCE = "/models/voices/original-british-chief-of-staff.wav"
PROFILES = {
    "british-balanced": {
        "label": "British · composed",
        "reference": REFERENCE,
        "exaggeration": 0.25,
        "cfg_weight": 0.5,
        "temperature": 0.7,
        "seed": 27,
    },
    "british-warm": {
        "label": "British · warmer",
        "reference": REFERENCE,
        "exaggeration": 0.35,
        "cfg_weight": 0.45,
        "temperature": 0.75,
        "seed": 27,
    },
    "british-restrained": {
        "label": "British · restrained",
        "reference": REFERENCE,
        "exaggeration": 0.15,
        "cfg_weight": 0.6,
        "temperature": 0.65,
        "seed": 27,
    },
    "legacy-reference": {
        "label": "Previous robotic reference · comparison",
        "reference": "/app/original-british-reference.wav",
        "exaggeration": 0.35,
        "cfg_weight": 0.4,
        "temperature": 0.8,
        "seed": 27,
    },
}
SCRIPTS = {
    "briefing": "Good evening, sir. You have three items worth your attention. Kevin needs a response before tomorrow morning, your ten o'clock meeting has moved thirty minutes earlier, and I've prepared the notes you asked for. Everything else can wait.",
    "wit": "That is technically possible, sir. I would not recommend it, but I suspect that has never stopped you before.",
}


def profile(identifier):
    if identifier not in PROFILES:
        raise ValueError("Unknown voice candidate")
    return {
        **PROFILES[identifier],
        "id": identifier,
        "model": MODEL,
        "checkpoint": CHECKPOINT,
        "repetition_penalty": 1.2,
        "min_p": 0.05,
        "top_p": 1.0,
    }
