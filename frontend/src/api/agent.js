// Agent API helpers.
//
// Uses the shared ``fetchApi`` from ``client.js`` for consistency with
// the rest of the frontend (error classification, Content-Type header).

import { fetchApi } from './client';

export async function listSessions() {
  return fetchApi('/agent/sessions');
}

export async function createSession(name) {
  return fetchApi('/agent/sessions', {
    method: 'POST',
    body: JSON.stringify({ name: name || undefined }),
  });
}

export async function deleteSession(id) {
  return fetchApi(`/agent/sessions/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
}

export async function getNotebook(sessionId) {
  return fetchApi(`/agent/sessions/${encodeURIComponent(sessionId)}/notebook`);
}

export async function getAssumptions(sessionId) {
  return fetchApi(`/agent/sessions/${encodeURIComponent(sessionId)}/assumptions`);
}
