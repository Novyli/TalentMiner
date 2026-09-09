#!/usr/bin/env python3
"""GQI Talent Radar launcher."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8899, reload=False)
