export const FX_CAPABILITIES = Object.freeze({
  commands: "commands.v1",
  listEffects: "effects.list.v1",
  status: "effects.status.v1",
  settings: "settings.apply.v1",
  chaos: "chaos.trigger.v1",
});

export function hasFxCapability(device, capability) {
  return !!device?.online && Array.isArray(device.capabilities) && device.capabilities.includes(capability);
}

export function fxDurationBounds(effect) {
  const range = effect?.duration_ms;
  const min = Number(range?.min ?? range?.minimum ?? effect?.min_duration_ms ?? 100);
  const max = Number(range?.max ?? range?.maximum ?? effect?.max_duration_ms ?? 60000);
  const value = Number(effect?.default_duration_ms ?? range?.default ?? Math.min(Math.max(8000, min), max));
  return { min, max, value: Math.min(max, Math.max(min, value)) };
}

export function fxParameterEntries(effect) {
  return Array.isArray(effect?.parameters)
    ? effect.parameters.map(item => [item.id || item.name, item])
    : Object.entries(effect?.parameters && typeof effect.parameters === "object" ? effect.parameters : {});
}

export function normalizeFxParameter(meta = {}, value) {
  const type = meta.type || (typeof meta.default === "boolean" ? "boolean" : "number");
  const fallback = meta.default ?? (type === "boolean" ? false : meta.min ?? 0);
  const raw = value ?? fallback;
  if (type === "boolean") {
    if (typeof raw === "string") return ["true", "1", "yes", "on"].includes(raw.trim().toLowerCase());
    return raw === true || raw === 1;
  }
  const number = Number(raw);
  const safe = Number.isFinite(number) ? number : Number(fallback);
  return type === "integer" ? Math.trunc(safe) : safe;
}

export function normalizeFxParameters(effect, values = {}) {
  return Object.fromEntries(fxParameterEntries(effect).map(([key, meta]) => [
    key,
    normalizeFxParameter(meta && typeof meta === "object" ? meta : {}, values[key]),
  ]));
}

export function buildFxTrigger(effect, values = {}) {
  if (!effect?.id && !effect?.effect) throw new Error("Effect metadata is missing an id");
  const effectId = effect.id || effect.effect;
  const bounds = fxDurationBounds(effect);
  const duration = Number(values.duration_ms ?? bounds.value);
  const intensity = Number(values.intensity ?? effect.default_intensity ?? 0.7);
  if (!Number.isInteger(duration) || duration < bounds.min || duration > bounds.max) {
    throw new Error(`Duration must be ${bounds.min}-${bounds.max} ms`);
  }
  if (!Number.isFinite(intensity) || intensity < 0.05 || intensity > 1) {
    throw new Error("Intensity must be 0.05-1.0");
  }
  return {
    type: "trigger_effect",
    effect: effectId,
    duration_ms: duration,
    intensity,
    parameters: normalizeFxParameters(effect, values.parameters || {}),
    queue: !!values.queue,
  };
}

export function fxControlsState(device) {
  if (!device?.online) return { trigger: false, settings: false, chaos: false, reason: "OBS device is offline" };
  return {
    trigger: hasFxCapability(device, FX_CAPABILITIES.commands),
    settings: hasFxCapability(device, FX_CAPABILITIES.settings),
    chaos: hasFxCapability(device, FX_CAPABILITIES.chaos),
    reason: "",
  };
}

