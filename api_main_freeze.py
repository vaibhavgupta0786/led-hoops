"""Frozen entry for hoops-api (Nuitka onefile target).
Run: hoops-api  -> serves API + built ui/ on API_PORT.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from api.config import API_HOST, API_PORT
from api.main import app

if __name__ == "__main__":
    uvicorn.run(app, host=API_HOST, port=int(os.getenv("API_PORT", API_PORT)))
