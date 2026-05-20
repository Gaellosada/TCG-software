const API_BASE = '/api';

class ApiError extends Error {
  constructor(errorType, message, details = null) {
    super(message);
    this.name = 'ApiError';
    this.errorType = errorType;
    this.details = details;
  }
}

async function fetchApi(path, options = {}) {
  const { headers: userHeaders, ...rest } = options;
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...rest,
      headers: { 'Content-Type': 'application/json', ...userHeaders },
    });
  } catch (err) {
    // Preserve abort semantics — callers rely on AbortError propagating
    // unchanged so they can distinguish user cancellation from real
    // network failure.
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError(
      'network_error',
      `Backend unreachable — is the server running on ${API_BASE}? (${err.message})`,
    );
  }

  if (!response.ok) {
    const error = await response.json().catch(() => ({
      error_type: 'unknown',
      message: response.statusText,
    }));
    throw new ApiError(error.error_type, error.message, error.details);
  }

  return response.json();
}

export { fetchApi, ApiError };
