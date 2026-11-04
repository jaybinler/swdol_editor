/** Derive the UI mode from a legacy-compatible operation without modifying the draft. */
export function operationMode(edit) {
  return edit.source_id === edit.target_id ? 'edit' : 'create';
}

/** Validate a new ID against both source tables and other operations; a suggestion reserves nothing. */
export function targetIdError(value, baselineIds, edits, editingTarget = null) {
  if (!Number.isInteger(value) || value < 1 || value > 32767) return '新道具 ID 必须是 1 至 32767 的整数。';
  if (baselineIds.has(value)) return `ID ${value} 已被游戏记录占用，请选择空闲 ID。`;
  if (edits.some(edit => edit.target_id === value && edit.target_id !== editingTarget)) {
    return `ID ${value} 已被其他草稿占用，请选择空闲 ID。`;
  }
  return '';
}

/** Replace only the operation explicitly opened by the user; collisions never overwrite another draft. */
export function replaceDraftOperation(edits, edit, editingTarget = null) {
  const remaining = edits.filter(entry => entry.target_id !== editingTarget);
  if (remaining.some(entry => entry.target_id === edit.target_id)) {
    throw new Error(`ID ${edit.target_id} 已被其他草稿占用，请打开该草稿继续编辑。`);
  }
  return [...remaining, edit];
}

/** Project current form values for display only; original guard takes priority and is never written. */
export function displayProperties(properties, baselineStats, currentStats) {
  const current = (key, fallback) => Number.isInteger(currentStats[key]) ? currentStats[key] : fallback;
  const guard = properties.hidden_attributes.find(field => field.key === 'guard');
  const evasion = current('evasion', baselineStats.evasion);
  const followsEvasion = properties.applicable && guard?.value === 0 &&
    Number.isInteger(baselineStats.evasion) && baselineStats.evasion !== 0;
  const hidden = properties.hidden_attributes.map(field => {
    if (field.key === 'guard' && followsEvasion) {
      return {...field, value: evasion, display_source: 'evasion'};
    }
    if (field.key === 'hp_recovery' || field.key === 'mp_recovery') {
      return {...field, value: properties.applicable ? current(field.key, field.value) : null};
    }
    return {...field};
  });
  hidden.unshift({key: 'evasion', label: '回避率',
    value: properties.applicable ? evasion : null, source: properties.applicable ? 'template' : 'not_applicable'});
  return {...properties, hidden_attributes: hidden,
    resistances: properties.resistances.map(field => ({...field,
      value: properties.applicable ? current(`resist_${field.key}`, field.value) : null}))};
}
