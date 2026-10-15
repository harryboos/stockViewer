export const CONCEPT_MODEL = 'deepseek-flash';
export const CONCEPT_MODEL_LABEL = 'DeepSeek Flash · MAX';

// Historical entries retain their stored model and prompt version. Do not
// relabel earlier GLM reports when the model for new research changes.
export function researchModelLabel(model?: string | null, promptVersion?: string | null, maxResearch = false) {
  if (!model) return '模型未记录';
  const known = model === 'deepseek-flash' ? 'DeepSeek Flash' : model === 'glm-5.3' ? 'GLM 5.3' : null;
  if (!known) return model;
  return `${known}${maxResearch || promptVersion?.endsWith('-max') ? ' · MAX' : ''}`;
}

export function activeConceptModelLabel(run?: { model?: string | null; provider?: string; promptVersion?: string | null } | null) {
  const provider = run?.provider?.split(':').at(-1);
  return researchModelLabel(run?.model || (provider === 'glm' ? 'glm-5.3' : CONCEPT_MODEL), run?.promptVersion, true);
}
