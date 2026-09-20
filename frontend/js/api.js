// js/api.js
// ---------------------------------------------------------------------------
// The single place this frontend talks to the backend.
//
// Everything above this file deals in plain objects and never sees a status
// code, an envelope, or a token. That matters for more than tidiness: token
// refresh has to be centralised, or a dozen call sites each grow their own
// half-correct retry.
// ---------------------------------------------------------------------------

// --- token storage ---------------------------------------------------------
//
// The backend sets the refresh token as an HttpOnly, Secure, SameSite=Lax
// cookie scoped to /api/v1/auth. The browser sends it automatically on every
// request to that path. The frontend only stores the short-lived access token
// in sessionStorage. Nothing in localStorage is XSS-accessible anymore.

const ACCESS_TOKEN_KEY = "bn_access_token";
const USER_KEY = "bn_user";

const TokenStore = {
  getAccess() {
    try {
      return sessionStorage.getItem(ACCESS_TOKEN_KEY);
    } catch (_) {
      return null;
    }
  },
  getRefresh() {
    // Refresh token lives in an HttpOnly cookie; not accessible from JS.
    return null;
  },
  set(accessToken, _refreshToken) {
    try {
      if (accessToken) sessionStorage.setItem(ACCESS_TOKEN_KEY, accessToken);
    } catch (_) {
      /* private browsing: the session simply will not persist */
    }
  },
  setUser(user) {
    try {
      sessionStorage.setItem(USER_KEY, JSON.stringify(user));
    } catch (_) {}
  },
  getUser() {
    try {
      return JSON.parse(sessionStorage.getItem(USER_KEY) || "null");
    } catch (_) {
      return null;
    }
  },
  clear() {
    try {
      sessionStorage.removeItem(ACCESS_TOKEN_KEY);
      sessionStorage.removeItem(USER_KEY);
      localStorage.removeItem("bn_role");
    } catch (_) {}
  },
  isSignedIn() {
    return Boolean(this.getAccess());
  },
};

// --- errors ----------------------------------------------------------------

// Carries the backend's own error code and details, so a caller can react to
// `report_already_linked` specifically rather than string-matching a message.
class ApiError extends Error {
  constructor(message, { status = 0, code = "unknown_error", details = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  // Validation failures arrive as { field: "message" }; flatten for display.
  fieldMessages() {
    if (!this.details || typeof this.details !== "object") return [];
    return Object.entries(this.details)
      .filter(([, message]) => typeof message === "string" && message)
      .map(([field, message]) => `${field}: ${message}`);
  }
}

// --- refresh ---------------------------------------------------------------
//
// Single-flight. If three requests 401 at once they must not fire three
// refreshes: Phase 3 rotates the refresh token on every use, so the second
// would present an already-rotated token and sign the user out. The first
// caller starts a refresh; the rest await the same promise.
let refreshInFlight = null;

async function refreshAccessToken() {
  const refreshToken = TokenStore.getRefresh();
  if (!refreshToken) return false;

  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const response = await fetch(`${API_BASE_URL}/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
        const payload = await response.json().catch(() => null);
        if (!response.ok || !payload || payload.status !== "success") return false;

        TokenStore.set(payload.data.access_token, payload.data.refresh_token);
        return true;
      } catch (_) {
        return false;
      } finally {
        // Cleared on the next tick, so every concurrent awaiter sees this result.
        setTimeout(() => {
          refreshInFlight = null;
        }, 0);
      }
    })();
  }

  return refreshInFlight;
}

// --- core request ----------------------------------------------------------

async function request(path, { method = "GET", body, auth = true, retry = true } = {}) {
  const headers = {};
  const isFormData = body instanceof FormData;

  // Never set Content-Type for FormData: the browser must add its own
  // multipart boundary, and overriding it makes the body unparseable.
  if (body !== undefined && !isFormData) headers["Content-Type"] = "application/json";

  if (auth) {
    const token = TokenStore.getAccess();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers,
      body: isFormData ? body : body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (_) {
    // fetch only rejects on network failure, so this really is "no server".
    throw new ApiError(
      "Could not reach the server. Check that the backend is running.",
      { code: "network_error" }
    );
  }

  // A 401 means the access token expired; refresh and replay exactly once.
  // `retry` guards against a loop when the replay 401s again.
  if (response.status === 401 && auth && retry) {
    if (await refreshAccessToken()) {
      return request(path, { method, body, auth, retry: false });
    }
    TokenStore.clear();
  }

  if (response.status === 204) return null;

  const payload = await response.json().catch(() => null);

  if (!response.ok || !payload || payload.status === "error") {
    const error = (payload && payload.error) || {};
    throw new ApiError(error.message || `Request failed (${response.status})`, {
      status: response.status,
      code: error.code || "http_error",
      details: error.details || null,
    });
  }

  // Unwrap the locked { status, data } envelope so callers never see it.
  return payload.data;
}

// --- verbs -----------------------------------------------------------------

function apiGet(path, options = {}) {
  return request(path, { ...options, method: "GET" });
}

function apiPost(path, body, options = {}) {
  return request(path, { ...options, method: "POST", body });
}

function apiPatch(path, body, options = {}) {
  return request(path, { ...options, method: "PATCH", body });
}

function apiDelete(path, options = {}) {
  return request(path, { ...options, method: "DELETE" });
}

// Build `?a=1&b=2`, skipping blanks so an untouched filter is simply absent
// rather than sent as an empty string the backend would have to interpret.
function queryString(params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    search.append(key, value);
  });
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

// --- shared error presentation --------------------------------------------

// One place decides what a user sees when a call fails, so every page reports
// problems the same way instead of inventing its own wording.
function reportApiError(error, fallback = "Something went wrong.") {
  console.error(error);

  if (!(error instanceof ApiError)) {
    if (typeof showToast === "function") showToast(fallback, "error");
    return fallback;
  }

  const fields = error.fieldMessages();
  const message = fields.length ? fields.join(" · ") : error.message || fallback;

  if (typeof showToast === "function") showToast(message, "error");
  return message;
}
