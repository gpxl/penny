"""Visual test: toggle show_sonnet via dashboard API and capture menubar screenshots.

Usage: python3 scripts/test_show_sonnet.py

Requires: screen capture permission for the terminal running this script.
"""

import json
import subprocess
import time
import urllib.request
from pathlib import Path

OUT = Path("/tmp/penny-visual-test")
OUT.mkdir(exist_ok=True)


def get_port():
    p = Path.home() / ".penny" / ".dashboard_port"
    return int(p.read_text().strip())


def wait_for_dashboard(port, retries=10):
    for i in range(retries):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=2)
            return True
        except Exception:
            print(f"  Waiting for dashboard... ({i+1}/{retries})")
            time.sleep(2)
    return False


def post_config(port, patch):
    data = json.dumps(patch).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/config",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def get_config(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=5) as r:
        return json.loads(r.read())


def capture_menubar(name):
    """Capture the right side of the menubar where Penny lives."""
    path = OUT / f"{name}.png"
    # Capture right 500px x 40px of screen (system tray area)
    subprocess.run(
        ["screencapture", "-R", "1012,0,500,40", "-x", str(path)],
        check=True,
    )
    print(f"  Captured: {path}")
    return path


def main():
    port = get_port()
    print(f"Dashboard on port {port}")
    if not wait_for_dashboard(port):
        print("FAIL: Dashboard not reachable!")
        return

    # Get current state
    cfg = get_config(port)
    current = cfg["config"].get("menubar", {}).get("show_sonnet", True)
    print(f"Current show_sonnet: {current}")

    # Step 1: Set show_sonnet=true, capture
    print("\n--- Step 1: show_sonnet=true ---")
    resp = post_config(port, {"menubar": {"show_sonnet": True}})
    print(f"  API response show_sonnet: {resp['config']['menubar']['show_sonnet']}")
    time.sleep(1)
    cap_on = capture_menubar("01_sonnet_on")

    # Step 2: Set show_sonnet=false, capture
    print("\n--- Step 2: show_sonnet=false ---")
    resp = post_config(port, {"menubar": {"show_sonnet": False}})
    print(f"  API response show_sonnet: {resp['config']['menubar']['show_sonnet']}")
    time.sleep(1)
    cap_off = capture_menubar("02_sonnet_off")

    # Step 3: Set show_sonnet=true again, capture
    print("\n--- Step 3: show_sonnet=true (back on) ---")
    resp = post_config(port, {"menubar": {"show_sonnet": True}})
    print(f"  API response show_sonnet: {resp['config']['menubar']['show_sonnet']}")
    time.sleep(1)
    cap_on2 = capture_menubar("03_sonnet_on_again")

    # Compare file sizes (different bar counts = different image sizes)
    size_on = cap_on.stat().st_size
    size_off = cap_off.stat().st_size
    size_on2 = cap_on2.stat().st_size
    print(f"\n--- Results ---")
    print(f"  sonnet ON:    {size_on:,} bytes")
    print(f"  sonnet OFF:   {size_off:,} bytes")
    print(f"  sonnet ON v2: {size_on2:,} bytes")

    if size_on == size_off:
        print("\n  FAIL: ON and OFF screenshots are identical — toggle has no visual effect!")
    else:
        print("\n  PASS: ON and OFF screenshots differ — toggle changes the menubar!")

    # Check config.yaml on disk
    config_path = Path.home() / ".penny" / "config.yaml"
    import yaml
    with config_path.open() as f:
        disk_cfg = yaml.safe_load(f)
    disk_val = disk_cfg.get("menubar", {}).get("show_sonnet")
    print(f"\n  config.yaml on disk: show_sonnet={disk_val}")

    # Check penny logs for applyConfigPatch evidence
    log_path = Path.home() / ".penny" / "logs" / "launchd.log"
    if log_path.exists():
        lines = log_path.read_text().splitlines()
        patch_lines = [l for l in lines[-50:] if "config patch" in l.lower() or "applyConfigPatch" in l]
        make_img_lines = [l for l in lines[-50:] if "_make_status_image" in l]
        if patch_lines:
            print(f"\n  Log evidence (config patch):")
            for l in patch_lines[-5:]:
                print(f"    {l}")
        else:
            print(f"\n  WARNING: No 'config patch' log entries found in last 50 lines!")
            print(f"  This means applyConfigPatch_ is NOT being called by the app.")
        if make_img_lines:
            print(f"\n  Log evidence (_make_status_image):")
            for l in make_img_lines[-5:]:
                print(f"    {l}")

    print(f"\nScreenshots saved to {OUT}/")
    print("Open them to visually verify: open /tmp/penny-visual-test/")


if __name__ == "__main__":
    main()
