#!/usr/bin/env python3
"""Check the resolved shipping configs, not just the package source text.

Run with the release-pinned ESPHome CLI and PyYAML on PATH, as in the
Validate workflow: python3 tools/test-default-fan-profiles.py
"""

from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
PRESETS = {"silent", "balanced", "aggressive", "server"}
CURVE_NUMBERS = {
    f"curve_{kind}{index}"
    for kind in ("temp", "speed")
    for index in range(1, 6)
}
AUTO_SWITCHES = {f"auto_control_fan{index}" for index in range(1, 5)}


def resolved(path):
    result = subprocess.run(
        ["esphome", "config", str(path)], cwd=ROOT,
        capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr + result.stdout)
    # The CLI has already resolved !include/!extend. BaseLoader treats the
    # remaining ESPHome scalars (including !lambda) as data, never as code.
    return yaml.load(result.stdout, Loader=yaml.BaseLoader)


def check_profiles(path, expected_restore_mode):
    config = resolved(path)
    switches = {item["id"]: item for item in config.get("switch", [])}
    assert AUTO_SWITCHES <= switches.keys(), f"{path}: missing auto controls"
    for switch_id in AUTO_SWITCHES:
        assert switches[switch_id]["restore_mode"] == expected_restore_mode, (
            f"{path}: unexpected initial state for {switch_id}"
        )

    numbers = {item["id"]: item for item in config.get("number", [])}
    assert CURVE_NUMBERS <= numbers.keys(), f"{path}: missing curve tuning"
    assert all(numbers[key]["restore_value"] == "true" for key in CURVE_NUMBERS)

    buttons = {item["id"]: item for item in config.get("button", [])}
    for preset in PRESETS:
        button_id = f"curve_preset_{preset}"
        assert button_id in buttons, f"{path}: missing {button_id}"
        button = buttons[button_id]
        assert (
            button.get("internal", "false") == "false"
            and button.get("disabled_by_default", "false") == "false"
        ), (
            f"{path}: {button_id} is not enabled in Home Assistant"
        )
        assert len(button["on_press"]) == 1, f"{path}: duplicate preset actions"
        actions = button["on_press"][0]["then"]
        updated = [action["number.set"]["id"] for action in actions[:-1]]
        assert len(updated) == 10 and set(updated) == CURVE_NUMBERS, (
            f"{path}: {button_id} must set every curve point before applying it"
        )
        assert actions[-1]["component.update"]["id"] == "curve_output_value"
    print(f"PASS {path}: four presets, persistent tuning, {expected_restore_mode}")


def main():
    subprocess.run(["python3", "tools/localize-examples.py"], cwd=ROOT, check=True)
    configs = sorted(
        path for path in ROOT.glob("fancontroller-rev*.yaml")
        if ".qc." not in path.name
    )
    assert len(configs) == 12, "Expected six standard and six factory configs"
    for path in configs:
        check_profiles(path.relative_to(ROOT), "RESTORE_DEFAULT_OFF")

    # The shared defaults must not change explicit module users or custom
    # hardware-only builds. These examples resolve against the working tree.
    check_profiles(
        Path(".test-build/with-temperature-curve-rev-3.1.yaml"),
        "RESTORE_DEFAULT_ON",
    )
    hardware_only = resolved(Path(".test-build/basic-rev-3.1.yaml"))
    switch_ids = {item["id"] for item in hardware_only.get("switch", [])}
    assert not AUTO_SWITCHES.intersection(switch_ids), (
        "Hardware-only builds unexpectedly include automatic curve control"
    )
    print("PASS hardware-only build: no automatic curve control added")


if __name__ == "__main__":
    main()
