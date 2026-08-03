(function () {
  const ENGINEER_COLORS = [
    '#1565c0', '#2e7d32', '#c62828', '#6a1b9a', '#ef6c00',
    '#00838f', '#5d4037', '#ad1457', '#558b2f', '#283593'
  ];

  let mapInstance = null;
  let mapPolylines = [];
  let mapMarkers = [];

  const apiBaseEl = document.getElementById('apiBase');
  const jsonInputEl = document.getElementById('jsonInput');
  const summaryEl = document.getElementById('summary');
  const summaryGridEl = document.getElementById('summaryGrid');
  const mapEl = document.getElementById('map');
  const mapLegendEl = document.getElementById('mapLegend');
  const engineerCardsEl = document.getElementById('engineerCards');
  const unassignedSectionEl = document.getElementById('unassignedSection');
  const unassignedTitleEl = document.getElementById('unassignedTitle');
  const unassignedListEl = document.getElementById('unassignedList');

  function getApiBase() {
    const v = (apiBaseEl && apiBaseEl.value) || '';
    const base = v.replace(/\/+$/, '').trim();
    if (base) return base;
    if (typeof window !== 'undefined' && window.location) {
      var port = window.location.port;
      if (port === '4200') return 'http://localhost:8000';
      return window.location.origin;
    }
    return 'http://localhost:8000';
  }

  function showError(msg) {
    const existing = document.querySelector('.error-toast');
    if (existing) existing.remove();
    const div = document.createElement('div');
    div.className = 'error-toast';
    div.textContent = msg;
    document.body.appendChild(div);
    setTimeout(function () { div.remove(); }, 5000);
  }

  function isResultJson(obj) {
    return obj && typeof obj === 'object' && Array.isArray(obj.engineer_routes) && obj.summary != null;
  }

  function isPayloadJson(obj) {
    return obj && typeof obj === 'object' && Array.isArray(obj.engineers) && Array.isArray(obj.jobs);
  }

  function loadSamplePayload() {
    const sample = {
      break_duration_min: 15,
      engineers: [
        {
          engineer_id: 'ENG001',
          base_location: { lat: 12.9716, lng: 77.5946 },
          shift_start: '09:00',
          shift_end: '18:00',
          break_window: { start: '13:00', end: '13:30' },
          overtime_allowed: true,
          skills: ['fiber', 'electrical'],
          skill_ratings: { fiber: 5, electrical: 4 },
          max_jobs_per_shift: 8,
          workflows: ['Breakfix', 'Installation'],
          locations: ['Hebbal', 'Whitefield', 'Koramangala']
        },
        {
          engineer_id: 'ENG002',
          base_location: { lat: 12.9352, lng: 77.6245 },
          shift_start: '09:00',
          shift_end: '18:00',
          break_window: { start: '13:00', end: '13:30' },
          overtime_allowed: true,
          skills: ['electrical'],
          skill_ratings: { electrical: 5 },
          max_jobs_per_shift: 6,
          workflows: ['Breakfix'],
          locations: ['ECity', 'HSR', 'Koramangala']
        },
        {
          engineer_id: 'ENG003',
          base_location: { lat: 13.001, lng: 77.57 },
          shift_start: '09:00',
          shift_end: '18:00',
          break_window: { start: '13:00', end: '13:30' },
          overtime_allowed: true,
          skills: ['fiber', 'network'],
          skill_ratings: { fiber: 4, network: 5 },
          max_jobs_per_shift: 6,
          workflows: ['Installation'],
          locations: ['Hebbal', 'Whitefield']
        }
      ],
      jobs: [
        { job_id: 'JOB1', location: { lat: 12.98, lng: 77.60 }, location_name: 'Hebbal', required_skills: ['fiber'], priority: 'P1', sla_deadline: '2026-03-16T14:00:00', estimated_duration_min: 60, workflow_type: 'Breakfix' },
        { job_id: 'JOB2', location: { lat: 12.99, lng: 77.62 }, location_name: 'Whitefield', required_skills: ['fiber'], priority: 'P2', sla_deadline: '2026-03-16T15:00:00', estimated_duration_min: 45, workflow_type: 'Installation' },
        { job_id: 'JOB3', location: { lat: 12.97, lng: 77.58 }, location_name: 'Koramangala', required_skills: ['electrical'], priority: 'P2', sla_deadline: '2026-03-16T16:00:00', estimated_duration_min: 60, workflow_type: 'Breakfix' },
        { job_id: 'JOB4', location: { lat: 12.93, lng: 77.62 }, location_name: 'HSR', required_skills: ['network'], priority: 'P3', sla_deadline: '2026-03-16T18:00:00', estimated_duration_min: 40, workflow_type: 'Installation' },
        { job_id: 'JOB5', location: { lat: 12.91, lng: 77.60 }, location_name: 'ECity', required_skills: ['fiber'], priority: 'P1', sla_deadline: '2026-03-16T13:30:00', estimated_duration_min: 90, workflow_type: 'Breakfix' },
        { job_id: 'JOB6', location: { lat: 12.90, lng: 77.65 }, location_name: 'ECity', required_skills: ['electrical'], priority: 'P2', sla_deadline: '2026-03-16T17:00:00', estimated_duration_min: 50, workflow_type: 'Breakfix' }
      ]
    };
    jsonInputEl.value = JSON.stringify(sample, null, 2);
  }

  async function runOptimize() {
    let obj;
    try {
      obj = JSON.parse(jsonInputEl.value || '{}');
    } catch (e) {
      showError('Invalid JSON in input.');
      return;
    }
    if (!isPayloadJson(obj)) {
      showError('Input must be a payload with "engineers" and "jobs" arrays.');
      return;
    }
    const base = getApiBase();
    try {
      const res = await fetch(base + '/optimize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(obj)
      });
      if (!res.ok) {
        const err = await res.text();
        throw new Error(err || 'Request failed');
      }
      const data = await res.json();
      render(data);
    } catch (e) {
      showError(e.message || 'Optimization request failed. Is the API running at ' + base + '?');
    }
  }

  function renderFromPaste() {
    let obj;
    try {
      obj = JSON.parse(jsonInputEl.value || '{}');
    } catch (e) {
      showError('Invalid JSON.');
      return;
    }
    if (!isResultJson(obj)) {
      showError('Paste a result JSON with "engineer_routes" and "summary".');
      return;
    }
    render(obj);
  }

  function renderSummary(summary) {
    if (!summary) return;
    summaryGridEl.innerHTML = [
      { label: 'Total jobs', value: summary.total_jobs },
      { label: 'Assigned', value: summary.assigned_jobs },
      { label: 'Unassigned', value: summary.unassigned_jobs },
      { label: 'SLA met', value: summary.sla_met },
      { label: 'SLA at risk', value: summary.sla_at_risk },
      { label: 'Total travel (km)', value: summary.total_travel_km },
      { label: 'Avg utilization %', value: summary.avg_utilization_pct }
    ].map(function (x) {
      return '<div class="summary-item"><div class="value">' + escapeHtml(String(x.value)) + '</div><div class="label">' + escapeHtml(x.label) + '</div></div>';
    }).join('');
    summaryEl.hidden = false;
  }

  function escapeHtml(s) {
    const div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML;
  }

  function initMap() {
    if (mapInstance) return mapInstance;
    mapInstance = new google.maps.Map(mapEl, {
      center: { lat: 12.97, lng: 77.59 },
      zoom: 11,
      mapTypeControl: true,
      streetViewControl: false,
      fullscreenControl: true
    });
    return mapInstance;
  }

  function clearMapOverlays() {
    mapPolylines.forEach(function (p) { p.setMap(null); });
    mapMarkers.forEach(function (m) { m.setMap(null); });
    mapPolylines = [];
    mapMarkers = [];
  }

  function lightenColor(hex, pct) {
    var num = parseInt(hex.replace('#', ''), 16);
    var r = Math.min(255, (num >> 16) + (255 - (num >> 16)) * pct);
    var g = Math.min(255, ((num >> 8) & 0x00FF) + (255 - ((num >> 8) & 0x00FF)) * pct);
    var b = Math.min(255, (num & 0x0000FF) + (255 - (num & 0x0000FF)) * pct);
    return 'rgb(' + Math.round(r) + ',' + Math.round(g) + ',' + Math.round(b) + ')';
  }

  function svgDataUrl(svg) {
    return 'data:image/svg+xml;charset=UTF-8,' + encodeURIComponent(svg);
  }

  function baseLocationIcon(color) {
    var light = lightenColor(color, 0.75);
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">' +
      '<circle cx="16" cy="16" r="14" fill="' + light + '" stroke="' + color + '" stroke-width="2"/>' +
      '<path fill="' + color + '" d="M16 8l-6 6v8h4v-6h4v6h4v-8z"/>' +
      '</svg>';
    return { url: svgDataUrl(svg), scaledSize: new google.maps.Size(28, 28), anchor: new google.maps.Point(14, 14) };
  }

  function ticketLocationIcon(color) {
    var light = lightenColor(color, 0.7);
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">' +
      '<path fill="' + light + '" stroke="' + color + '" stroke-width="1.5" d="M6 6h20v20H6zm2 2v16h16V8zm3 2h2v2h-2zm0 4h2v2h-2zm0 4h2v2h-2zm8-8h2v2h-2zm0 4h2v2h-2zm0 4h2v2h-2z"/>' +
      '</svg>';
    return { url: svgDataUrl(svg), scaledSize: new google.maps.Size(26, 26), anchor: new google.maps.Point(13, 13) };
  }

  function drawMapRoutes(engineerRoutes) {
    if (typeof google === 'undefined' || !google.maps) {
      mapLegendEl.textContent = 'Google Maps loading…';
      return;
    }
    clearMapOverlays();
    const map = initMap();
    const bounds = new google.maps.LatLngBounds();

    (engineerRoutes || []).forEach(function (route, idx) {
      const color = ENGINEER_COLORS[idx % ENGINEER_COLORS.length];
      const path = [];
      const start = route.start_location;
      if (start && start.lat != null && start.lng != null) {
        path.push({ lat: start.lat, lng: start.lng });
      }
      (route.route || []).forEach(function (stop) {
        const loc = stop.location;
        if (loc && loc.lat != null && loc.lng != null) {
          path.push({ lat: loc.lat, lng: loc.lng });
        }
      });
      path.forEach(function (p) { bounds.extend(p); });

      if (path.length >= 2) {
        const polyline = new google.maps.Polyline({
          path: path,
          strokeColor: color,
          strokeWeight: 4,
          strokeOpacity: 0.45,
          map: map
        });
        mapPolylines.push(polyline);
      }

      path.forEach(function (point, pathIdx) {
        const isDepot = pathIdx === 0;
        const jobId = !isDepot && route.route && route.route[pathIdx - 1] ? route.route[pathIdx - 1].job_id : null;
        const marker = new google.maps.Marker({
          position: point,
          map: map,
          icon: isDepot ? baseLocationIcon(color) : ticketLocationIcon(color),
          zIndex: isDepot ? 100 : 50,
          title: isDepot ? ('Base: ' + route.engineer_id) : (jobId || 'Job')
        });
        mapMarkers.push(marker);
      });
    });

    if (bounds.getNorthEast && bounds.getSouthWest()) {
      map.fitBounds(bounds, { top: 60, right: 60, bottom: 60, left: 60 });
    }

    mapLegendEl.innerHTML = (engineerRoutes || []).map(function (route, idx) {
      const color = ENGINEER_COLORS[idx % ENGINEER_COLORS.length];
      return '<div class="item"><span class="dot" style="background:' + color + '"></span><span>' + escapeHtml(route.engineer_id) + '</span></div>';
    }).join('');
  }

  function getInitials(engineerId) {
    const m = engineerId.match(/([A-Z])/g);
    return (m && m.slice(0, 2).join('')) || engineerId.slice(0, 2);
  }

  function renderEngineerCard(route, colorIndex) {
    const color = ENGINEER_COLORS[colorIndex % ENGINEER_COLORS.length];
    const breakSlot = route.break || {};
    const items = [];

    items.push({
      type: 'depot',
      time: route.shift_start || '09:00',
      travel: null,
      label: 'Depot start',
      desc: 'Base',
      location: route.start_location ? (route.start_location.lat + ', ' + route.start_location.lng) : ''
    });

    function formatTravelSegment(min, km) {
      var parts = [];
      if (min != null && min !== '') parts.push(Number(min) + ' min');
      if (km != null && km !== '') parts.push(Number(km).toFixed(1) + ' km');
      return parts.length ? parts.join(' – ') : '';
    }

    (route.route || []).forEach(function (stop, i) {
      var isSlot = stop.slot != null;
      var travel;
      if (isSlot) {
        travel = stop.travel_from_prev_km != null && stop.travel_from_prev_km > 0
          ? Number(stop.travel_from_prev_km).toFixed(1) + ' km' : '';
      } else {
        travel = formatTravelSegment(stop.travel_from_prev_min, stop.travel_from_prev_km);
      }
      items.push({
        type: 'job',
        isSlot: isSlot,
        slotNum: isSlot ? stop.slot : null,
        slotLabel: isSlot ? (stop.slot_label || '') : '',
        time: isSlot ? '' : (stop.arrival_time || ''),
        travel: travel,
        jobId: stop.job_id,
        priority: stop.priority,
        slaMet: stop.sla_met,
        desc: stop.workflow_type || 'Job',
        duration: isSlot ? '' : (stop.estimated_duration_min != null ? stop.estimated_duration_min + ' min' : ''),
        location: stop.location_name || (stop.location ? (stop.location.lat + ', ' + stop.location.lng) : '')
      });
    });

    var hasBreak = breakSlot && Object.keys(breakSlot).length > 0;
    if (hasBreak) {
      var gapMin = breakSlot.gap_min;
      items.push({
        type: 'break',
        time: gapMin != null ? gapMin + ' min gap' : (breakSlot.start || '13:00') + '–' + (breakSlot.end || '13:30'),
        travel: null,
        label: 'Gap between jobs',
        desc: gapMin != null ? gapMin + ' min gap after each ticket' : 'Break window'
      });
    }

    const utilPct = route.utilization_pct != null ? Math.min(100, Math.round(route.utilization_pct)) : 0;
    const shiftText = (route.shift_start || '09:00') + '–' + (route.shift_end || '18:00');
    const otText = route.overtime_min > 0 ? 'OT used: ' + route.overtime_min + ' min' : 'OT allowed';

    let html = '<div class="engineer-card">';
    html += '<div class="card-header">';
    html += '<div class="avatar" style="background:' + color + '">' + escapeHtml(getInitials(route.engineer_id)) + '</div>';
    html += '<div class="card-meta">';
    html += '<div class="name">' + escapeHtml(route.engineer_id) + '</div>';
    html += '<div class="util">' + utilPct + '% util</div>';
    html += '<div class="util-bar"><div class="util-fill" style="width:' + utilPct + '%;background:' + color + '"></div></div>';
    html += '<div class="shift">Shift ' + escapeHtml(shiftText) + '</div>';
    html += '<div class="base">Base: ' + (route.start_location ? (route.start_location.lat.toFixed(4) + ', ' + route.start_location.lng.toFixed(4)) : '–') + '</div>';
    html += '<div class="ot">' + escapeHtml(otText) + '</div>';
    html += '</div></div>';
    html += '<div class="timeline">';

    items.forEach(function (it, i) {
      const isBreak = it.type === 'break';
      const isDepot = it.type === 'depot';
      const isSlot = !!it.isSlot;
      const showLine = i < items.length - 1;
      html += '<div class="timeline-item ' + it.type + (isSlot ? ' slot-item' : '') + '">';
      html += '<div class="timeline-marker">';
      if (isSlot) {
        html += '<span class="slot-badge">' + it.slotNum + '</span>';
      } else {
        html += '<span class="dot"></span>';
      }
      if (showLine) html += '<span class="line"></span>';
      html += '</div>';
      html += '<div class="timeline-body">';
      if (isSlot) {
        var slotTime = it.slotLabel ? ' (' + escapeHtml(it.slotLabel) + ')' : '';
        html += '<div class="slot-header">Slot-' + it.slotNum + slotTime + '</div>';
      } else if (isDepot) {
        html += '<div class="time">' + escapeHtml(it.time) + ' – ' + escapeHtml(it.label) + '</div>';
      } else if (!isBreak) {
        html += '<div class="time">' + escapeHtml(it.time || '') + '</div>';
      }
      if (it.travel) html += '<div class="travel">' + escapeHtml(it.travel) + '</div>';
      if (it.jobId) {
        html += '<div class="job-id">' + escapeHtml(it.jobId) + '</div>';
        html += '<div class="tags">';
        if (it.priority) html += '<span class="tag ' + (it.priority.toLowerCase()) + '">' + escapeHtml(it.priority) + '</span>';
        html += '<span class="tag ' + (it.slaMet ? 'sla-ok' : 'sla-risk') + '">' + (it.slaMet ? 'SLA ok' : 'SLA risk!') + '</span>';
        html += '</div>';
      }
      if (isBreak) html += '<div class="tags"><span class="tag break">break ' + escapeHtml(it.time) + '</span></div>';
      html += '<div class="desc">' + escapeHtml(it.desc || it.label || '') + (it.duration ? ' · ' + it.duration : '') + '</div>';
      if (it.location) html += '<div class="location">' + escapeHtml(it.location) + '</div>';
      html += '</div></div>';
    });

    html += '</div></div>';
    return html;
  }

  function renderUnassigned(unassigned) {
    if (!unassigned || unassigned.length === 0) {
      unassignedSectionEl.hidden = true;
      return;
    }
    unassignedTitleEl.textContent = 'Unassigned jobs (' + unassigned.length + ')';
    unassignedListEl.innerHTML = unassigned.map(function (u) {
      return '<div class="unassigned-item">' +
        '<span class="job-id">' + escapeHtml(u.job_id) + '</span>' +
        '<span class="reason">' + escapeHtml(u.reason || '') + '</span>' +
        '</div>';
    }).join('');
    unassignedSectionEl.hidden = false;
  }

  function render(data) {
    const summary = data.summary;
    const engineerRoutes = data.engineer_routes || [];
    const unassigned = data.unassigned || [];

    renderSummary(summary);
    drawMapRoutes(engineerRoutes);
    engineerCardsEl.innerHTML = engineerRoutes.map(function (r, i) {
      return renderEngineerCard(r, i);
    }).join('');
    renderUnassigned(unassigned);
  }

  document.getElementById('loadSample').addEventListener('click', loadSamplePayload);
  document.getElementById('runOptimize').addEventListener('click', runOptimize);
  document.getElementById('renderResult').addEventListener('click', renderFromPaste);
})();
