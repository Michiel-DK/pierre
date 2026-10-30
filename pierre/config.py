"""Model names and the API key. The only configuration the showcase needs."""
import os

GEMINI_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GEMINI_MATCHING_MODEL = "gemini-2.5-flash"
GEMINI_EMBEDDING_MODEL = "models/gemini-embedding-001"
