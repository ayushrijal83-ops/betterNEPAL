// js/api.js
// Thin API wrapper. Uses mock data for now. Swap to real fetch calls later.

const USE_MOCK = true;

async function apiGet(path) {
  if (USE_MOCK) return mockGet(path);
  const res = await fetch(API_URL + path);
  if (!res.ok) throw new Error("API error: " + res.status);
  return res.json();
}

async function apiPost(path, body) {
  if (USE_MOCK) return mockPost(path, body);
  const res = await fetch(API_URL + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error("API error: " + res.status);
  return res.json();
}

// --- Mock backend ---
function mockGet(path) {
  if (path.startsWith("/reports")) {
    return Promise.resolve(Object.values(MOCK_REPORTS));
  }
  return Promise.resolve(null);
}

function mockPost(path, body) {
  if (path === "/reports") {
    const id = "BNP" + Math.floor(1000 + Math.random() * 9000);
    const report = {
      id,
      title: body.title || "Untitled report",
      category: body.category || "Other",
      severity: body.severity || "Medium",
      status: "Reported",
      location: body.location || "Unknown",
      coordinates: body.coordinates || "—",
      date: new Date().toISOString(),
      reporter: body.reporter || "Current User",
      description: body.description || "",
      ai: {
        classification: "Pending AI classification",
        hazard: "Pending",
        confidence: 0.0,
        department: "Pending routing",
        requiresVerification: true,
      },
      timeline: [
        { label: "Reported by user", time: "Just now", state: "done" },
        { label: "AI-assisted classification", time: "Pending", state: "active" },
        { label: "Authority verification", time: "—", state: "" },
        { label: "Repair assigned", time: "—", state: "" },
        { label: "Repair in progress", time: "—", state: "" },
        { label: "Completed", time: "—", state: "" },
      ],
    };
    MOCK_REPORTS[id] = report;
    return Promise.resolve(report);
  }
  return Promise.resolve(null);
}