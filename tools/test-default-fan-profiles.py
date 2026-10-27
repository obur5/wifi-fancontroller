#!/usr/bin/env python3
"""Check the resolved shipping configs, not just the package source text.

Run with the release-pinned ESPHome CLI and PyYAML on PATH, as in the
Validate workflow: python3 tools/test-default-fan-profiles.py
"""

from pathlib import Path
import subprocess
import tempfile

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
    assert all(numbers[key]["mode"] == "BOX" for key in CURVE_NUMBERS)

    profiles = {item["id"]: item for item in config.get("select", [])}
    profile = profiles["fan_profile"]
    assert profile["options"] == ["Silent", "Balanced", "Aggressive", "Server", "Custom"]
    assert profile.get("entity_category", "") == ""
    assert "lambda" in profile and "restore_value" not in profile
    assert "set_action" in profile
    assert not any(key.endswith("_control_mode") for key in profiles), (
        f"{path}: duplicate fan-mode dropdowns are exposed"
    )
    for switch_id in AUTO_SWITCHES:
        if expected_restore_mode == "RESTORE_DEFAULT_OFF":
            assert switches[switch_id].get("entity_category", "") == ""
            assert switches[switch_id]["on_turn_on"][0]["then"][0]["component.update"]["id"] == "curve_output_value"

    buttons = {item["id"]: item for item in config.get("button", [])}
    for preset in PRESETS:
        button_id = f"curve_preset_{preset}"
        assert button_id in buttons, f"{path}: missing {button_id}"
        button = buttons[button_id]
        expected_internal = "true" if expected_restore_mode == "RESTORE_DEFAULT_OFF" else "false"
        assert button.get("internal", "false") == expected_internal, (
            f"{path}: duplicate preset-button visibility is incorrect"
        )
        assert len(button["on_press"]) == 1, f"{path}: duplicate preset actions"
        actions = button["on_press"][0]["then"]
        updated = [action["number.set"]["id"] for action in actions if "number.set" in action]
        assert len(updated) == 10 and set(updated) == CURVE_NUMBERS, (
            f"{path}: {button_id} must set every curve point before applying it"
        )
        assert actions[-2]["component.update"]["id"] == "curve_output_value"
        assert actions[-1]["component.update"]["id"] == "fan_profile"
        assert all("number.set" in action for action in actions[:10])
    print(f"PASS {path}: four presets, persistent tuning, {expected_restore_mode}")
    return config


def check_profile_detection(config):
    """Run the actual device lambda against the preset actions and custom edits."""
    profile = next(item for item in config["select"] if item["id"] == "fan_profile")
    buttons = {item["id"]: item for item in config["button"]}
    source = [
        "#include <cassert>", "#include <cmath>", "#include <limits>",
        "#include <optional>", "#include <string>", "#define id(x) x",
        "struct Number { float state = 0; };",
        "bool curve_profile_custom = false;",
        "using std::optional;",
        "struct Profile { bool known = false; std::string value;",
        "bool has_state() { return known; }",
        "std::string current_option() { return value; } };",
        "Profile fan_profile;",
        "Number " + ", ".join(sorted(CURVE_NUMBERS)) + ";",
        "std::optional<std::string> detect_profile() {",
        profile["lambda"], "}", "int main() {",
    ]
    for preset in profile["options"][:-1]:
        actions = buttons[f"curve_preset_{preset.lower()}"]["on_press"][0]["then"]
        for action in actions:
            if "number.set" in action:
                number = action["number.set"]
                source.append(f'{number["id"]}.state = {float(number["value"])}f;')
        source.append(f'assert(detect_profile().value() == "{preset}");')
        source.extend([
            "curve_profile_custom = true;",
            'assert(detect_profile().value() == "Custom");',
            "curve_profile_custom = false;",
        ])
    source.extend([
        'fan_profile.known = true; fan_profile.value = "Server";',
        "assert(!detect_profile().has_value());",
        "curve_speed5.state -= 1;",
        'assert(detect_profile().value() == "Custom");',
        "curve_temp1.state = std::numeric_limits<float>::quiet_NaN();",
        'assert(detect_profile().value() == "Custom");',
        "}",
    ])
    with tempfile.TemporaryDirectory(prefix="fan-profile-test-") as directory:
        path = Path(directory) / "profile.cpp"
        binary = Path(directory) / "profile-test"
        path.write_text("\n".join(source))
        subprocess.run(["c++", "-std=c++17", str(path), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
    print("PASS profile detection: all presets, custom edits, explicit Custom, invalid values")


def main():
    subprocess.run(["python3", "tools/localize-examples.py"], cwd=ROOT, check=True)
    configs = sorted(
        path for path in ROOT.glob("fancontroller-rev*.yaml")
        if ".qc." not in path.name
    )
    assert len(configs) == 12, "Expected six standard and six factory configs"
    for path in configs:
        config = check_profiles(path.relative_to(ROOT), "RESTORE_DEFAULT_OFF")
    check_profile_detection(config)

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
