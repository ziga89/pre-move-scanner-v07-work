#!/usr/bin/env sh
# Pre-Move Scanner launcher for macOS / Linux (same steps as run_windows.bat).
#   ./run.sh                 live scanner on http://127.0.0.1:8000
#   ./run.sh sim             offline demo (port 8001)
#   ./run.sh selftest [...]  live self-test          ./run.sh walletcheck [...]  live wallet-provider check
#   ./run.sh test            automated tests         ./run.sh release [...]      clean release ZIP in dist/
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "[setup] Creating virtual environment .venv ..."
  python3 -m venv .venv
fi
echo "[setup] Installing / updating dependencies ..."
"$PY" -m pip install --disable-pip-version-check -q -r requirements.txt
"$PY" tools/bootstrap.py
VERSION=$("$PY" tools/bootstrap.py --version)
ACTION=${1:-live}
[ $# -gt 0 ] && shift
case "$ACTION" in
  selftest) exec "$PY" tools/selftest.py "$@" ;;
  walletcheck) exec "$PY" tools/wallet_check.py "$@" ;;
  stress) exec "$PY" tools/feed_stress.py "$@" ;;
  sim) echo "Starting OFFLINE DEMO v$VERSION on http://127.0.0.1:8001"; PMS_MODE=sim exec "$PY" -m uvicorn server.app:app --host 127.0.0.1 --port 8001 ;;
  import) exec "$PY" tools/import_v06.py "$@" ;;
  test) "$PY" -m pip install --disable-pip-version-check -q -r requirements-dev.txt; exec "$PY" -m unittest discover -s tests -t . -v ;;
  release) exec "$PY" tools/make_release.py "$@" ;;
  *) echo "Starting Pre-Move Scanner v$VERSION - live mode on http://127.0.0.1:8000"; exec "$PY" -m uvicorn server.app:app --host 0.0.0.0 --port 8000 ;;
esac
