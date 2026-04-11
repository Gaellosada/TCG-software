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
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });

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
