/**
 * Shared constants for the Agent page.
 *
 * Model definitions live here so they are not duplicated between
 * ChatPanel (ModelPicker) and AgentPage (default model state).
 */

export const AGENT_MODELS = [
  { id: 'claude-sonnet-4-6', label: 'Sonnet' },
  { id: 'claude-opus-4-6', label: 'Opus' },
];

export const DEFAULT_MODEL = 'claude-sonnet-4-6';
