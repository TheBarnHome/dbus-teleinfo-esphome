#!/bin/sh
# The pure layers need nothing but the standard library, so this runs on a
# laptop, in CI, and on the Venus OS target itself before an install.
set -eu
cd "$(dirname "$0")"
python3 tests/test_teleinfo.py
python3 tests/test_esphome_http.py
python3 dbus-teleinfo.py --self-test
