// js/alerts.js

const MOCK_ALERTS = [
  {
    type: "critical",
    title: "Critical: Monsoon flood warning, Sunsari District",
    meta: "20 mins ago",
  },
  {
    type: "warning",
    title: "Warning: Bridge closure, Prithvi Highway, Malekhu",
    meta: "1 hour ago",
  },
  {
    type: "info",
    title: "Information: New road project Kalanki–Underpass Phase 2 started",
    meta: "3 hours ago",
  },
];

function getAlerts() {
  return MOCK_ALERTS;
}

function renderAlertCard(a) {
  return `
    <div class="alert-card ${a.type}">
      <div>
        <div class="alert-title">${a.title}</div>
        <div class="alert-meta">${a.meta}</div>
      </div>
    </div>
  `;
}

function renderAlertList(containerId, alerts) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = alerts.map(renderAlertCard).join("");
}