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
    expected_options = ["Silent", "Balanced", "Aggressive", "Server", "Custom"]
    if expected_restore_mode == "RESTORE_DEFAULT_OFF":
        expected_options.append("Manual")
    assert profile["options"] == expected_options
    assert profile.get("entity_category", "") == ""
    assert "lambda" in profile and "restore_value" not in profile
    assert "set_action" in profile
    assert not any(key.endswith("_control_mode") for key in profiles), (
        f"{path}: duplicate fan-mode dropdowns are exposed"
    )
    for switch_id in AUTO_SWITCHES:
        if expected_restore_mode == "RESTORE_DEFAULT_OFF":
            assert switches[switch_id]["internal"] == "true"

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
        "bool curve_automatic_mode = true;",
        "using std::optional;",
        "struct Profile { bool known = false; std::string value;",
        "bool has_state() { return known; }",
        "std::string current_option() { return value; } };",
        "Profile fan_profile;",
        "Number " + ", ".join(sorted(CURVE_NUMBERS)) + ";",
        "std::optional<std::string> detect_profile() {",
        profile["lambda"], "}", "int main() {",
    ]
    for preset in ["Silent", "Balanced", "Aggressive", "Server"]:
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
    if "Manual" in profile["options"]:
        source.extend([
            "curve_automatic_mode = false;",
            'assert(detect_profile().value() == "Manual");',
            "curve_automatic_mode = true;",
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


def check_manual_control(config):
    """Execute actual mode, curve-output and fan-hook lambdas with native fakes."""
    profile = next(item for item in config["select"] if item["id"] == "fan_profile")
    output = next(item for item in config["output"] if item["id"] == "curve_proxy_output")
    sensor = next(item for item in config["sensor"] if item["id"] == "curve_output_value")
    fans = {item["id"]: item for item in config["fan"]}
    buttons = {item["id"]: item for item in config["button"]}
    source = [
        "#include <cassert>", "#include <cmath>", "#include <string>",
        "#include <optional>", "#define id(x) x", "using std::optional;",
        "bool curve_profile_custom = false, curve_automatic_mode = true, curve_applying_output = false;",
        "struct Number { float state = 0; };",
        "Number " + ", ".join(sorted(CURVE_NUMBERS)) + ";",
        "Number fancontroller_temperature{30};",
        "struct Switch { bool state = false; void turn_on() { state = true; } void turn_off() { state = false; } };",
        "Switch auto_control_fan1, auto_control_fan2, auto_control_fan3, auto_control_fan4;",
        "struct Fan; struct Call { Fan *fan; bool state; float speed = -1; void set_speed(float x) { speed = x; } void perform(); };",
        "struct Fan { bool state = false; float speed = 0; void (*on)(); void (*off)();",
        "Call turn_on() { return {this, true}; } Call turn_off() { return {this, false}; } };",
        "void Call::perform() { bool changed = fan->state != state; fan->state = state;",
        "if (speed >= 0) fan->speed = speed; if (changed) { if (state) fan->on(); else fan->off(); } }",
        "struct Profile { bool has_state() { return false; } std::string current_option() { return {}; } void update() {} };",
        "Profile fan_profile;",
        "struct Output { void update(); }; Output curve_output_value;",
        "struct Button { void (*apply)(); void press() { apply(); } };",
    ]
    for i in range(1, 5):
        source.extend([
            f"void fan{i}_on(); void fan{i}_off();",
            f"Fan fan{i}{{false, 0, fan{i}_on, fan{i}_off}};",
            f"bool usr_override_fan{i} = false; float fan{i}_safety_floor = 0;",
        ])
    for preset in sorted(PRESETS):
        source.extend([
            f"void preset_{preset}();",
            f"Button curve_preset_{preset}{{preset_{preset}}};",
        ])
    source.extend([
        "float calculate_curve() {", sensor["lambda"], "}",
        "void apply_curve(float state) {", output["write_action"]["then"][0]["lambda"], "}",
        "void Output::update() { curve_applying_output = true; apply_curve(calculate_curve()/100.0f); curve_applying_output = false; }",
        "void choose_profile(std::string x) {", profile["set_action"]["then"][0]["lambda"], "}",
    ])
    for i in range(1, 5):
        for action, suffix in (("on_turn_on", "on"), ("on_turn_off", "off")):
            source.extend([
                f"void fan{i}_{suffix}() {{",
                fans[f"fan{i}"][action][0]["then"][0]["lambda"], "}",
            ])
    for preset in sorted(PRESETS):
        source.append(f"void preset_{preset}() {{")
        for action in buttons[f"curve_preset_{preset}"]["on_press"][0]["then"]:
            if "number.set" in action:
                number = action["number.set"]
                source.append(f'{number["id"]}.state = {float(number["value"])}f;')
        source.extend(["curve_profile_custom = false; curve_output_value.update(); }"])
    source.extend([
        "int main() {",
        "fan1.state = true; fan1.speed = 55; fan3.state = true; fan3.speed = 40;",
        "auto_control_fan1.turn_on(); auto_control_fan3.turn_on();",
        'choose_profile("Manual");',
        "assert(!curve_automatic_mode && !auto_control_fan1.state && !auto_control_fan3.state);",
        "assert(fan1.state && fan1.speed == 55 && fan3.state && fan3.speed == 40);",
        "curve_output_value.update(); assert(fan1.speed == 55 && fan3.speed == 40);",
        "fan1.speed = 65; curve_output_value.update(); assert(fan1.speed == 65);",
        'choose_profile("Server");',
        "assert(curve_automatic_mode && auto_control_fan1.state && auto_control_fan3.state);",
        "assert(fan1.speed == 75 && fan3.speed == 75 && !fan2.state && !fan4.state);",
        # A zero curve output pauses a fan but must leave it eligible to restart.
        "curve_speed1.state = curve_speed2.state = curve_speed3.state = curve_speed4.state = curve_speed5.state = 0;",
        "curve_output_value.update(); assert(!fan1.state && auto_control_fan1.state);",
        'choose_profile("Server"); assert(fan1.state && fan1.speed == 75);',
        # A user's Off command must remain Off across curve ticks/profile changes.
        "fan1.turn_off().perform(); assert(!auto_control_fan1.state);",
        'choose_profile("Aggressive"); assert(!fan1.state && !auto_control_fan1.state);',
        "fan1.turn_on().perform(); assert(fan1.state && auto_control_fan1.state && fan1.speed > 60);",
        'choose_profile("Manual"); fan4.turn_on().perform(); fan4.speed = 42;',
        "curve_output_value.update(); assert(!auto_control_fan4.state && fan4.speed == 42);",
        'choose_profile("Custom"); assert(curve_automatic_mode && curve_profile_custom && auto_control_fan4.state);',
        "}",
    ])
    # Verify the actual YAML wraps all output writes with the callback guard.
    actions = sensor["on_value"][0]["then"]
    assert actions[0]["lambda"] == "id(curve_applying_output) = true;"
    assert "output.set_level" in actions[1]
    assert actions[2]["lambda"] == "id(curve_applying_output) = false;"
    with tempfile.TemporaryDirectory(prefix="fan-manual-test-") as directory:
        path = Path(directory) / "manual.cpp"
        binary = Path(directory) / "manual-test"
        path.write_text("\n".join(source))
        subprocess.run(["c++", "-std=c++17", str(path), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
    print("PASS Manual: fixed speeds, curve resume, user Off, curve pause/restart, Custom")


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
    check_manual_control(config)

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
