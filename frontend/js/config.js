// js/config.js

const API_URL = "http://localhost:8000/api";

const NAV_ITEMS = {
  citizen: [
    { label: "Dashboard", href: "dashboard.html", icon: "home" },
    { label: "Map", href: "map.html", icon: "map" },
    { label: "Report Issue", href: "report.html", icon: "alert" },
    { label: "My Reports", href: "my-reports.html", icon: "list" },
    { label: "Alerts", href: "alerts.html", icon: "bell" },
    { label: "Profile", href: "profile.html", icon: "user" },
  ],
  guide: [
    { label: "Dashboard", href: "dashboard.html", icon: "home" },
    { label: "Report Issue", href: "report.html", icon: "alert" },
    { label: "My Reports", href: "my-reports.html", icon: "list" },
    { label: "Trekking Routes", href: "routes.html", icon: "trail" },
    { label: "Map", href: "map.html", icon: "map" },
    { label: "Alerts", href: "alerts.html", icon: "bell" },
    { label: "Profile", href: "profile.html", icon: "user" },
  ],
  authority: [
    { label: "Dashboard", href: "dashboard.html", icon: "home" },
    { label: "Incidents", href: "incidents.html", icon: "alert" },
    { label: "Reports", href: "reports.html", icon: "list" },
    { label: "Projects", href: "projects.html", icon: "folder" },
    { label: "Map", href: "map.html", icon: "map" },
    { label: "Alerts", href: "alerts.html", icon: "bell" },
    { label: "Profile", href: "profile.html", icon: "user" },
  ],
  contractor: [
    { label: "Dashboard", href: "dashboard.html", icon: "home" },
    { label: "Assigned Projects", href: "assigned-projects.html", icon: "folder" },
    { label: "Progress Update", href: "progress-update.html", icon: "upload" },
    { label: "Profile", href: "profile.html", icon: "user" },
  ],
  admin: [
    { label: "Dashboard", href: "dashboard.html", icon: "home" },
    { label: "Users", href: "users.html", icon: "user" },
    { label: "Authorities", href: "authorities.html", icon: "shield" },
    { label: "Departments", href: "departments.html", icon: "folder" },
    { label: "Reports", href: "reports.html", icon: "list" },
    { label: "Incidents", href: "incidents.html", icon: "alert" },
    { label: "Projects", href: "projects.html", icon: "folder" },
    { label: "Map", href: "map.html", icon: "map" },
    { label: "System", href: "system.html", icon: "settings" },
  ],
};

const ICONS = {
  home: '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  map: '<path d="M9 20l-5.447-2.724A1 1 0 0 1 3 16.382V5.618a1 1 0 0 1 1.447-.894L9 7m0 13l6-3m-6 3V7m6 10l4.553 2.276A1 1 0 0 0 21 18.382V7.618a1 1 0 0 0-.553-.894L15 4m0 13V4m0 0L9 7"/>',
  alert: '<path d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  bell: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 0 1-3.46 0"/>',
  user: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  trail: '<path d="M4 20l4-8 4 4 4-12 4 8"/>',
  folder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>',
};

const ROLES = ["citizen", "guide", "authority", "contractor", "admin"];