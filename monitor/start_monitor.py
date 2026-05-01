"""
monitor/start_monitor.py — Launch the NWO web dashboard.

Usage:
    python -m monitor.start_monitor
    python -m monitor.start_monitor --port 9000

Then open http://localhost:8765 (or your PC's LAN IP) in any browser.
"""

import argparse
import sys
from pathlib import Path

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="NWO Web Monitor")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0 = all interfaces)")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    args = parser.parse_args()

    print(f"\n  NWO Monitor starting on http://0.0.0.0:{args.port}")
    print(f"  Local:   http://localhost:{args.port}")
    print(f"  Network: find your IP via `ipconfig` > IPv4 Address")
    print(f"  Press Ctrl+C to stop.\n")

    # Kill any process already holding the port
    import subprocess, os
    try:
        result = subprocess.run(
            f'netstat -ano | findstr ":{args.port} " | findstr LISTENING',
            shell=True, capture_output=True, text=True
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            pid = parts[-1] if parts else None
            if pid and pid.isdigit() and int(pid) != os.getpid():
                subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
                print(f"  Killed stale process PID {pid} on port {args.port}")
    except Exception:
        pass

    uvicorn.run(
        "monitor.dashboard:app",
        host=args.host,
        port=args.port,
        reload=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
