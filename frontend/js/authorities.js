// js/authorities.js

const MOCK_AUTHORITIES = [
  {
    id: "AUTH-001",
    name: "Department of Roads — Gandaki Province",
    district: "Kaski",
    province: "Gandaki",
    category: "Road / Bridge",
    contact: "+977-61-XXXXXX",
    email: "roads.gandaki@gov.np",
  },
  {
    id: "AUTH-002",
    name: "District Disaster Management Committee — Manang",
    district: "Manang",
    province: "Gandaki",
    category: "Disaster",
    contact: "+977-66-XXXXXX",
    email: "ddmc.manang@gov.np",
  },
  {
    id: "AUTH-003",
    name: "NEA Lalitpur Division",
    district: "Lalitpur",
    province: "Bagmati",
    category: "Electricity",
    contact: "+977-1-XXXXXXX",
    email: "nea.lalitpur@nea.org.np",
  },
];

function getAuthorities() {
  return MOCK_AUTHORITIES;
}

function getAuthorityById(id) {
  return MOCK_AUTHORITIES.find((a) => a.id === id) || MOCK_AUTHORITIES[0];
}

function renderAuthoritiesTable(tbodyId, authorities) {
  const el = document.getElementById(tbodyId);
  if (!el) return;
  el.innerHTML = authorities
    .map(
      (a) => `
    <tr>
      <td>${a.name}</td>
      <td>${a.district}</td>
      <td>${a.province}</td>
      <td>${a.category}</td>
      <td>${a.contact}</td>
      <td>${a.email}</td>
    </tr>`
    )
    .join("");
}