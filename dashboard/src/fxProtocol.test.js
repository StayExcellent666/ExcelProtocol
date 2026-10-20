import test from "node:test";
import assert from "node:assert/strict";
import { FX_CAPABILITIES, buildFxTrigger, fxControlsState, hasFxCapability } from "./fxProtocol.js";

test("offline devices never expose effect controls", () => {
  const device = { online: false, capabilities: Object.values(FX_CAPABILITIES) };
  assert.deepEqual(fxControlsState(device), {
    trigger: false, settings: false, chaos: false, reason: "OBS device is offline",
  });
});

test("controls are gated by negotiated capabilities", () => {
  const device = { online: true, capabilities: [FX_CAPABILITIES.commands, FX_CAPABILITIES.listEffects] };
  assert.equal(hasFxCapability(device, FX_CAPABILITIES.commands), true);
  assert.equal(fxControlsState(device).trigger, true);
  assert.equal(fxControlsState(device).settings, false);
  assert.equal(fxControlsState(device).chaos, false);
});

test("effect payload comes from plugin metadata and enforces its duration limits", () => {
  const effect = { id: "fog", min_duration_ms: 500, max_duration_ms: 5000, default_duration_ms: 1200 };
  assert.deepEqual(buildFxTrigger(effect, { intensity: 0.6 }), {
    type: "trigger_effect", effect: "fog", duration_ms: 1200,
    intensity: 0.6, parameters: {}, queue: false,
  });
  assert.throws(() => buildFxTrigger(effect, { duration_ms: 6000 }), /500-5000/);
});

