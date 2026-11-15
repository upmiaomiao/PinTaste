"""Shared database and attachment initialization without starting HTTP or jobs."""
from pathlib import Path
import sys


def initialize_runtime():
    core=str(Path(__file__).resolve().parent.parent/'model-chat')
    if core not in sys.path:
        sys.path.append(core)
    import attachments
    from . import store as db, integrated_identity, voice, expert_feedback, document_rag
    db.init()
    integrated_identity.init()
    voice.init()
    expert_feedback.init()
    attachments.UPLOAD_DIR=db.DATA/'uploads'
    document_rag.init()
