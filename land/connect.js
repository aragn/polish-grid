/* Grid connection configuration: a road-following cable route from a plot to its substation, the design (MV circuits or
   a 110 kV substation and transformers) and its costs as low–high ranges, with the operator's fee, the cash up front and
   the timeline.  Shared by the Ariadne demo and the polish-grid Land tab.

   Route: OpenStreetMap roads and tracks around the plot and the substation, read from the Overpass API when the user asks
   (one query per click), shortest path along them; watercourses and railways in the same query give the crossings.  When
   no road route exists, or it is much longer than a straight line, the route goes across the fields and every metre needs
   an easement.

   Law (checked 26 Nov 2026, consolidated texts from the Sejm's ELI service): Energy Law art. 7(8) (fee), 7(8a)–(8c1)
   (application fee, advance, security), 7(8g) (deadlines); grid regulation Dz.U. 2023 poz. 819 § 3 (groups); Building
   Law art. 29 (permits); Roads Act art. 40 (road lane); Civil Code art. 305¹ (easements).
   Costs: PLN, net, 2026, screening ranges; see CONNECT for the source of each figure. */
(function (root) {
  "use strict";
  const CONNECT = {
    mvCircuitMw: 8,                    // planning load of one 15–20 kV cable circuit (Al 240 mm² is ~10 MVA thermally)
    mv30CircuitMw: 20,                 // one 30 kV circuit, as used inside large plants
    hvAboveMw: 20,                     // above this, 110 kV: connection group II
    ehvAboveMw: 250,                   // above this, usually 220/400 kV to a PSE station
    mvCablePerKm: [0.2e6, 0.35e6],     // one 15–20 kV circuit in open trench: operators' 2026 rates of PLN 195–220/m; 200–230k/km for 3×70–120 (forum, 2019)
    mv30CablePerKm: [0.3e6, 0.55e6],   // ASSUMPTION: one 30 kV circuit, 3×1×240–400 mm² Al, trench included
    extraCircuitShare: 0.6,            // each further circuit in the same trench (cable and laying, no new trench)
    hvCablePerKm: [2.5e6, 4.0e6],      // TAURON Krzywa–Bolesławiec 2025: 13 km for over PLN 42 m (~3.2 m/km); PGE Łomianki–Czosnów 14.8 km double circuit, PLN 51 m
    hddMv: [30e3, 90e3],               // a trenchless crossing (HDD) per MV circuit: PLN 100–250 per metre of duct (2023 price lists), 150–300 m with pits; ASSUMPTION
    hddHv: [0.3e6, 0.9e6],             // ASSUMPTION: an HDD crossing for a 110 kV cable (three ducts, larger bore, cable joints nearby)
    mvBay: [0.3e6, 0.8e6],             // ASSUMPTION: an MV feeder bay in the operator's GPZ
    siteMvStation: [0.4e6, 1.0e6],     // ASSUMPTION: the plant's MV switching and metering station
    hvLineBay: [3e6, 7e6],             // ASSUMPTION: a 110 kV line bay in an existing GPZ (no published unit price; tenders such as ENEA's GPZ Morzyczyn bay give no value)
    hvSubstation: [12e6, 22e6],        // the plant's own 110/MV substation with one transformer: Elektrotim–Solartech, PLN 53 m for one GPO 110/20 kV with its 110 kV cable plus a second GPO; PGE's GPZ Czosnów ~PLN 18 m
    transformerMva: 63,                // one 110/MV unit; above that, another transformer
    extraTransformer: [4e6, 7e6],      // per further unit: Enea Operator, 129 transformers of 25–40 MVA for over PLN 444 m (~3.4 m each, installed); 63 MVA costs more
    ehvSubstation: [80e6, 150e6],      // ASSUMPTION: a 400(220)/MV substation and a bay at a PSE station
    easementPerM: [80, 250],           // ASSUMPTION: one-off, underground, farmland; set by a valuation report (no rate card in the law)
    detourFields: 1.15,                // a route across the fields, against the straight line
    applicationPerKw: 1, applicationMax: 1e5,                          // Energy Law art. 7(8b1)
    advancePerKw: 60, advanceMax: 6e6,                                 // art. 7(8a)–(8b), never above the expected fee
    securityPerKw: [30, 60], securityStepKw: 1e5, securityMax: 12e6,   // art. 7(8c1)
    leadTransformerMonths: [18, 30],   // European OEM lead times for 110 kV power transformers, 2024–26 (suppliers' and market reports)
  };
  // public Overpass instances listed on the OSM wiki, tried in turn (each with a 15 s timeout)
  const OVERPASS = ["https://overpass-api.de/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter", "https://overpass.kumi.systems/api/interpreter"];
  const ROAD_W = { trunk: 1.5, trunk_link: 1.5, primary: 1.2, primary_link: 1.2, secondary: 1.08, secondary_link: 1.08, tertiary: 1.04, tertiary_link: 1.04,
    unclassified: 1, road: 1, residential: 1.12, living_street: 1.15, service: 1.05, track: 1 };
  const PUBLIC = new Set(["trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified", "residential", "living_street", "road"]);

  const R = 6371008.8, rad = d => d * Math.PI / 180;
  const hav = (a, b) => { const dl = rad(b[0] - a[0]), dp = rad(b[1] - a[1]); const x = Math.sin(dp / 2) ** 2 + Math.cos(rad(a[1])) * Math.cos(rad(b[1])) * Math.sin(dl / 2) ** 2; return 2 * R * Math.asin(Math.sqrt(x)); };
  const add = (...rs) => rs.reduce((s, r) => [s[0] + r[0], s[1] + r[1]], [0, 0]);
  const mul = (r, k) => [r[0] * k, r[1] * k];
  const segX = (p1, p2, p3, p4) => {     // do segments p1p2 and p3p4 cross ([lon, lat], fine at this scale)
    const o = (a, b, c) => Math.sign((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]));
    return o(p1, p2, p3) !== o(p1, p2, p4) && o(p3, p4, p1) !== o(p3, p4, p2);
  };

  async function overpass(bbox) {
    const [w, s, e, n] = bbox.map(v => v.toFixed(5));
    const b = `${s},${w},${n},${e}`;
    const q = `[out:json][timeout:25];(way["highway"~"^(${Object.keys(ROAD_W).join("|")})$"](${b});way["waterway"~"^(river|stream|canal|ditch|drain)$"](${b});way["railway"~"^(rail|light_rail|narrow_gauge)$"](${b}););out body geom qt;`;
    let err;
    for (const url of OVERPASS) {
      const ac = new AbortController(), timer = setTimeout(() => ac.abort(), 15000);
      try {
        const r = await fetch(url, { method: "POST", body: "data=" + encodeURIComponent(q), headers: { "Content-Type": "application/x-www-form-urlencoded" }, signal: ac.signal });
        if (!r.ok) throw new Error(`${r.status}`);
        const els = (await r.json()).elements || [], out = { roads: [], water: [], rail: [] };
        for (const el of els) {
          if (el.type !== "way" || !el.geometry) continue;
          const g = el.geometry.map(p => [p.lon, p.lat]);
          if (el.tags.waterway) out.water.push([el.tags.waterway, el.tags.name || "", g]);
          else if (el.tags.railway) out.rail.push(g);
          else out.roads.push([el.tags.highway, g]);
        }
        return out;
      } catch (e) { err = e; } finally { clearTimeout(timer); }
    }
    throw err || new Error("Overpass unavailable");
  }

  // Shortest path on the road graph (Dijkstra with a binary heap).
  function shortest(adj, from, to) {
    const dist = new Map([[from, 0]]), prev = new Map(), heap = [[0, from]];
    const push = x => { heap.push(x); let i = heap.length - 1; while (i) { const p = (i - 1) >> 1; if (heap[p][0] <= heap[i][0]) break; [heap[p], heap[i]] = [heap[i], heap[p]]; i = p; } };
    const pop = () => { const top = heap[0], last = heap.pop(); if (heap.length) { heap[0] = last; let i = 0; for (;;) { const l = 2 * i + 1, r = l + 1; let m = i; if (l < heap.length && heap[l][0] < heap[m][0]) m = l; if (r < heap.length && heap[r][0] < heap[m][0]) m = r; if (m === i) break; [heap[m], heap[i]] = [heap[i], heap[m]]; i = m; } } return top; };
    while (heap.length) {
      const [d, u] = pop();
      if (u === to) break;
      if (d > dist.get(u)) continue;
      for (const [v, w] of adj.get(u) || []) { const nd = d + w; if (nd < (dist.has(v) ? dist.get(v) : Infinity)) { dist.set(v, nd); prev.set(v, u); push([nd, v]); } }
    }
    if (!dist.has(to)) return null;
    const path = [to]; while (path[0] !== from) path.unshift(prev.get(path[0]));
    return path;
  }

  /* The route from the plot to the substation.  ring: the plot outline [[lon, lat]...]; st: {lon, lat}. */
  async function route(ring, st, opts = {}) {
    const S = [st.lon, st.lat];
    let a = ring[0], best = Infinity;
    for (const v of ring) { const d = hav(v, S); if (d < best) { best = d; a = v; } }       // the plot's corner nearest the substation
    const straight = best;
    const pad = Math.max(600, straight * 0.35) / 111320, padLon = pad / Math.cos(rad(st.lat));
    const bbox = [Math.min(a[0], S[0]) - padLon, Math.min(a[1], S[1]) - pad, Math.max(a[0], S[0]) + padLon, Math.max(a[1], S[1]) + pad];
    // roads: a local file first (opts.roads: a promise or the data), then the Overpass API, else a straight line
    let net = null, note = null, source = "";
    if (straight > 150) {
      try { net = opts.roads ? await opts.roads : null; if (net) source = "file"; } catch (e) { net = null; }
      if (!net) { try { net = await overpass(bbox); source = "overpass"; } catch (e) { note = "OpenStreetMap roads couldn't be read just now: the route goes in a straight line across the fields"; } }
    }
    net = net || { roads: [], water: [], rail: [] };
    const inBox = g => g.some(([x, y]) => x >= bbox[0] && x <= bbox[2] && y >= bbox[1] && y <= bbox[3]);
    const coord = new Map(), adj = new Map(), water = [], rail = [];
    const key = c => `${c[0].toFixed(6)},${c[1].toFixed(6)}`;      // ways share a node where they share a point
    for (const [kind, name, g] of net.water || []) if (inBox(g)) water.push({ g, kind, name });
    for (const g of net.rail || []) if (inBox(g)) rail.push({ g });
    for (const [hw, g] of net.roads || []) {
      if (!inBox(g)) continue;
      const k = ROAD_W[hw] || 1.1, ids = g.map(key);
      ids.forEach((id, i) => coord.set(id, g[i]));
      for (let i = 1; i < ids.length; i++) {
        const u = ids[i - 1], v = ids[i], w = hav(g[i - 1], g[i]) * k;
        if (!adj.has(u)) adj.set(u, []); if (!adj.has(v)) adj.set(v, []);
        adj.get(u).push([v, w, hw]); adj.get(v).push([u, w, hw]);
      }
    }
    const nearest = p => { let id = null, d = Infinity; for (const [k, c] of coord) { const x = hav(p, c); if (x < d) { d = x; id = k; } } return [id, d]; };
    let path = null, method = "fields", onPublic = 0, onTrack = 0;
    if (adj.size) {
      // start from the road node nearest any corner of the plot (the cable leaves the plot where a road touches it)
      let sId = null, sD = Infinity, sV = a;
      for (const v of ring.length > 60 ? ring.filter((_, i) => i % Math.ceil(ring.length / 60) === 0) : ring) { const [id, d] = nearest(v); if (d < sD) { sD = d; sId = id; sV = v; } }
      const [eId, eD] = nearest(S);
      const ids = sId != null && eId != null ? shortest(adj, sId, eId) : null;
      if (ids) {
        const pts = [sV, ...ids.map(id => coord.get(id)), S];
        let len = 0; for (let i = 1; i < pts.length; i++) len += hav(pts[i - 1], pts[i]);
        // a road route is worth it unless it is far longer than going straight
        if (len <= Math.max(straight * 1.9, straight + 700) && sD < Math.max(400, straight * 0.5) && eD < 600) {
          path = pts; method = "roads";
          for (let i = 1; i < ids.length; i++) { const hw = (adj.get(ids[i - 1]).find(x => x[0] === ids[i]) || [])[2]; const l = hav(coord.get(ids[i - 1]), coord.get(ids[i])); if (PUBLIC.has(hw)) onPublic += l; else onTrack += l; }
        }
      }
    }
    // crossings of watercourses and railways (each a trenchless crossing and a consent)
    // where segment p1p2 crosses q1q2 ([lon, lat]), or null
    const hit = (p1, p2, q1, q2) => { if (!segX(p1, p2, q1, q2)) return null; const d = (p2[0] - p1[0]) * (q2[1] - q1[1]) - (p2[1] - p1[1]) * (q2[0] - q1[0]); if (!d) return p1; const t = ((q1[0] - p1[0]) * (q2[1] - q1[1]) - (q1[1] - p1[1]) * (q2[0] - q1[0])) / d; return [p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1])]; };
    // crossings, with those within 80 m of each other merged (parallel tracks of one railway, a stream and its ditch): one bore each
    const crossOf = pts => {
      const found = { river: [], ditch: [], rail: [] }, names = new Set();
      const addPt = (k, c) => { if (!found[k].some(o => hav(o, c) < 80)) found[k].push(c); };
      for (let i = 1; i < pts.length; i++) {
        const p1 = pts[i - 1], p2 = pts[i];
        for (const w of water) for (let j = 1; j < w.g.length; j++) { const c = hit(p1, p2, w.g[j - 1], w.g[j]); if (c) { const k = w.kind === "ditch" || w.kind === "drain" ? "ditch" : "river"; addPt(k, c); if (k === "river" && w.name) names.add(w.name); } }
        for (const r of rail) for (let j = 1; j < r.g.length; j++) { const c = hit(p1, p2, r.g[j - 1], r.g[j]); if (c) addPt("rail", c); }
      }
      return { river: found.river.length, ditch: found.ditch.length, rail: found.rail.length, names: [...names], points: [...found.river, ...found.ditch, ...found.rail].map(([x, y]) => [y, x]) };
    };
    // the straight route across the fields, always worked out: the plan takes whichever costs less
    const fKm = straight * CONNECT.detourFields / 1000;
    const fields = { path: [[a[1], a[0]], [S[1], S[0]]], km: fKm, straightKm: straight / 1000, method: "fields", source, onPublicKm: 0, onTrackKm: 0, offRoadKm: fKm, cross: crossOf([a, S]), note, start: [a[1], a[0]] };
    if (!path) return fields;
    let km = 0; for (let i = 1; i < path.length; i++) km += hav(path[i - 1], path[i]);
    km /= 1000;
    const offRoad = Math.max(0, km * 1000 - onPublic - onTrack);
    return { path: path.map(([lon, lat]) => [lat, lon]), km, straightKm: straight / 1000, method, source, onPublicKm: onPublic / 1000, onTrackKm: onTrack / 1000, offRoadKm: offRoad / 1000, cross: crossOf(path), note, start: [a[1], a[0]], alt: fields };
  }

  /* The cheaper of the road route and the route across the fields (on the middle of each range); the other is kept for comparison. */
  function plan(o, r) {
    const d = design({ ...o, route: r });
    if (!r.alt) return { r, d, other: null };
    const d2 = design({ ...o, route: r.alt }), mid = x => x.total[0] + x.total[1];
    return mid(d2) < mid(d) ? { r: r.alt, d: d2, other: { r, d } } : { r, d, other: { r: r.alt, d: d2 } };
  }

  /* The design and its costs.  o: { mw, mwh, kind: "bess" | "pv" | "wind" | "dc", usableHa, stName, route } */
  function design(o) {
    const C = CONNECT, mw = Math.max(0.1, o.mw), kw = mw * 1000, r = o.route, km = r.km;
    const crossings = r.cross.river + r.cross.rail + Math.min(r.cross.ditch, 6);
    const level = mw > C.ehvAboveMw ? "ehv" : mw > C.hvAboveMw ? "hv" : "mv";
    const items = [];   // [label, [lo, hi], who: "dev" | "op", detail]
    let what, circuits = 0, transformers = 0;
    if (level === "mv") {
      circuits = Math.max(1, Math.ceil(mw / C.mvCircuitMw));
      const cable = mul(C.mvCablePerKm, km * (1 + C.extraCircuitShare * (circuits - 1)));
      what = `${circuits} × 15–20 kV cable circuit${circuits > 1 ? "s" : ""} in one trench to the MV busbars at ${o.stName}`;
      items.push([`MV cable, ${circuits} circuit${circuits > 1 ? "s" : ""} × ${km.toFixed(2)} km`, cable, "dev", "3 × 1 × 240 mm² Al, open trench"]);
      if (crossings) items.push([`Trenchless crossings (${crossings})`, mul(C.hddMv, crossings * circuits), "dev", "watercourses, ditches, railways"]);
      items.push(["MV switching and metering station on the plot", C.siteMvStation, "dev", ""]);
      items.push([`MV feeder bay${circuits > 1 ? "s" : ""} at ${o.stName} (${circuits})`, mul(C.mvBay, circuits), "op", "built by the operator, charged in the fee"]);
    } else if (level === "hv") {
      transformers = Math.max(1, Math.ceil(mw * 1.1 / C.transformerMva));
      const mvColl = o.kind === "dc" ? "MV" : "30 kV";
      what = `the plant's own 110/${mvColl} substation on the plot (${transformers} × ${C.transformerMva} MVA transformer${transformers > 1 ? "s" : ""}), then a 110 kV cable underground to a line bay at ${o.stName}`;
      items.push([`110/${mvColl} substation on the plot`, add(C.hvSubstation, mul(C.extraTransformer, transformers - 1)), "dev", `${transformers} × ${C.transformerMva} MVA, 110 kV and MV switchgear, protection, control building`]);
      items.push([`110 kV cable × ${km.toFixed(2)} km`, mul(C.hvCablePerKm, km), "dev", "single circuit, XLPE, laying and joints"]);
      if (crossings) items.push([`Trenchless crossings (${crossings})`, mul(C.hddHv, crossings), "dev", "watercourses, ditches, railways"]);
      items.push([`110 kV line bay at ${o.stName}`, C.hvLineBay, "op", "built by the operator, charged in the fee"]);
    } else {
      what = `a 220/400 kV connection to a PSE station: the plant's own 400(220)/MV substation and a bay at the PSE station; the route to the nearest 220/400 kV station is set by PSE`;
      items.push(["400(220)/MV substation and PSE bay", C.ehvSubstation, "dev", "order of magnitude"]);
      items.push([`Cable × ${km.toFixed(2)} km (110 kV figure, indicative)`, mul(C.hvCablePerKm, km), "dev", ""]);
    }
    const easementM = Math.max(0, r.offRoadKm * 1000 + r.onTrackKm * 1000 * 0.5);   // across fields, and half the field tracks (often private)
    if (easementM > 1) items.push([`Easements, ~${(easementM / 1000).toFixed(2)} km of private land`, mul(C.easementPerM, easementM), "dev", "one-off, per valuation report"]);
    const total = add(...items.map(i => i[1]));
    const opWorks = add([0, 0], ...items.filter(i => i[2] === "op").map(i => i[1]));
    // the operator's fee: half its actual costs for storage (art. 7(8) pkt 6) and for renewables of 5 MW or less (pkt 3 lit. a); otherwise full (pkt 3, pkt 8)
    const share = o.kind === "bess" ? 0.5 : (o.kind === "pv" || o.kind === "wind") && mw <= 5 ? 0.5 : 1;
    const fee = mul(opWorks, share);
    const developer = add(...items.filter(i => i[2] === "dev").map(i => i[1]));
    const application = Math.min(kw * C.applicationPerKw, C.applicationMax);
    const advance = Math.min(kw * C.advancePerKw, C.advanceMax, Math.max(fee[1], application));
    const security = Math.min(C.securityPerKw[0] * Math.min(kw, C.securityStepKw) + C.securityPerKw[1] * Math.max(0, kw - C.securityStepKw), C.securityMax);
    const timeline = [
      ["Connection conditions", level === "mv" ? "120 days" : "150 days", "from a complete application (Energy Law art. 7(8g))"],
      ["Land rights and permits", level === "mv" ? "6–12 months" : "9–18 months", "easements, road-lane permit, building permit (notification only for networks up to 15 kV)"],
      ...(level !== "mv" ? [["Transformer delivery", `${C.leadTransformerMonths[0]}–${C.leadTransformerMonths[1]} months`, "order early: European lead times for 110 kV units"]] : []),
      ["Construction", level === "mv" ? "4–9 months" : "12–24 months", "cable, substation, bay, tests"],
    ];
    const permits = [
      level === "mv" ? "MV cable up to 15 kV: notification only (Building Law art. 29(1)(2)(a)); 20 or 30 kV: building permit" : "110 kV cable and the substation: building permit (not exempted by Building Law art. 29)",
      r.onPublicKm > 0.05 ? `~${r.onPublicKm.toFixed(1)} km in public road lanes: the road manager's permit and an annual fee (Roads Act art. 40)` : null,
      r.cross.river ? `${r.cross.river} watercourse crossing${r.cross.river > 1 ? "s" : ""}${r.cross.names.length ? ` (${r.cross.names.slice(0, 3).join(", ")})` : ""}: water-law consent (check)` : null,
      r.cross.ditch ? `${r.cross.ditch} ditch crossing${r.cross.ditch > 1 ? "s" : ""}: the drainage company's or water authority's agreement (check)` : null,
      r.cross.rail ? `${r.cross.rail} railway crossing${r.cross.rail > 1 ? "s" : ""}: the rail manager's agreement` : null,
      "underground cables aren't on the environmental-decision list; an overhead 110 kV line would be (EIA regulation § 3(1)(7))",
    ].filter(Boolean);
    let fits = null;
    if (o.usableHa != null) {
      if (o.kind === "bess") { const need = Math.max(0.3, (o.mwh || mw * 4) / 160 + (level === "mv" ? 0.05 : 0.4)); fits = [o.usableHa >= need, `${(o.mwh || mw * 4).toFixed(0)} MWh needs about ${need.toFixed(1)} ha with the substation and access (~160 MWh per ha of containers)`]; }
      else if (o.kind === "pv") { const mwp = o.usableHa * 0.9; fits = [mwp >= mw * 1.2, `about ${mwp.toFixed(0)} MWp fits on ${o.usableHa.toFixed(1)} ha (0.9 MWp per ha, fixed tilt); ${mw} MW AC wants ~${(mw * 1.2).toFixed(0)} MWp`]; }
      else if (o.kind === "wind") fits = [true, "turbine positions need a separate layout (spacing, 500 m from homes)"];
      else fits = [o.usableHa >= mw / 8, `about ${(mw / 8).toFixed(1)} ha for ${mw} MW of IT load at ~8 MW per ha of building and plant`];
    }
    return { level, what, circuits, transformers, items, total, developer, opWorks, fee, share, application, advance, security, timeline, permits, fits, group: level === "mv" ? "III" : level === "hv" ? "II" : "I" };
  }

  const pln = ([lo, hi]) => { const f = v => v >= 1e6 ? (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) : (v / 1e6).toFixed(2); return `PLN ${f(lo)}–${f(hi)} m`; };
  const plnOne = v => v >= 1e6 ? `PLN ${(v / 1e6).toFixed(1)} m` : `PLN ${Math.round(v / 1000).toLocaleString("en")}k`;
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* HTML for the result (uses the host page's CSS variables: --text, --muted, --border, --panel-2). */
  function html(r, d, other) {
    const row = (a, b, c) => `<tr><td style="padding:6px 8px 6px 0;border-top:1px solid var(--border)">${a}${c ? `<div style="color:var(--muted);font-size:11.5px">${c}</div>` : ""}</td><td style="padding:6px 0;border-top:1px solid var(--border);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums">${b}</td></tr>`;
    const how = r.method === "roads"
      ? `<b>${r.km.toFixed(2)} km</b> along roads and tracks (${r.straightKm.toFixed(2)} km straight): ${r.onPublicKm.toFixed(1)} km in public roads, ${r.onTrackKm.toFixed(1)} km on tracks${r.offRoadKm > 0.02 ? `, ${(r.offRoadKm * 1000).toFixed(0)} m across fields at the ends` : ""}`
      : `<b>${r.km.toFixed(2)} km</b> across the fields (${r.straightKm.toFixed(2)} km straight +15%), with easements all the way${r.note ? `; ${esc(r.note)}` : ""}`;
    return `<div style="font-size:13px;line-height:1.5">
      <div style="margin-bottom:8px"><b>${d.level === "mv" ? "Medium voltage" : d.level === "hv" ? "110 kV" : "220/400 kV"}</b>, connection group ${d.group}: ${esc(d.what)}.</div>
      <div style="margin-bottom:8px">Route: ${how}${other ? `; cheaper than ${other.r.method === "roads" ? `following the roads (${other.r.km.toFixed(2)} km, ${pln(other.d.total)})` : `going across the fields (${other.r.km.toFixed(2)} km with easements all the way, ${pln(other.d.total)})`}` : ""}. Crossings: ${r.cross.river} watercourse${r.cross.river === 1 ? "" : "s"}, ${r.cross.ditch} ditch${r.cross.ditch === 1 ? "" : "es"}, ${r.cross.rail} railway${r.cross.rail === 1 ? "" : "s"}.</div>
      ${d.fits ? `<div style="margin-bottom:8px">On the plot: ${d.fits[0] ? "fits" : "<b>tight</b>"}: ${esc(d.fits[1])}.</div>` : ""}
      <table style="width:100%;border-collapse:collapse;margin:6px 0 4px">${d.items.map(i => row(`${esc(i[0])}${i[2] === "op" ? ' <span style="color:var(--muted)">(operator)</span>' : ""}`, pln(i[1]), esc(i[3]))).join("")}
        ${row("<b>Total to connect</b>", `<b>${pln(d.total)}</b>`)}
        ${row(`Of which the operator's fee`, pln(d.fee), d.share === 0.5 ? "half its actual cost (storage, or renewables ≤ 5 MW: Energy Law art. 7(8))" : "its full actual cost (Energy Law art. 7(8))")}</table>
      <div style="margin:10px 0 4px"><b>Cash up front:</b> application fee ${plnOne(d.application)} (not refunded); advance ${plnOne(d.advance)} within 14 days of applying; security ${plnOne(d.security)} within 14 days of signing the agreement (art. 7(8a)–(8c1)).</div>
      <div style="margin:10px 0 4px"><b>Timeline:</b> ${d.timeline.map(t => `${esc(t[0])} <b>${esc(t[1])}</b>`).join(" · ")}.</div>
      <div style="margin:10px 0 4px"><b>Permits along the route:</b> ${d.permits.map(esc).join("; ")}.</div>
      <div style="color:var(--muted);font-size:11.5px;margin-top:8px">Screening ranges (PLN, net, 2026), not a quote; figures marked as assumptions in connect.js want a contractor's price. The operator sets the connection point, voltage and fee in the conditions. Roads: © OpenStreetMap contributors (ODbL), read from the Overpass API.</div></div>`;
  }

  /* Draw the configuration on a Leaflet map; returns the layer group. */
  function draw(map, r, d, stName) {
    const g = L.layerGroup().addTo(map);
    const color = d.level === "mv" ? "#2f6fdb" : "#8b5cf6";
    L.polyline(r.path, { color: "#000", weight: 7, opacity: 0.35 }).addTo(g);
    L.polyline(r.path, { color, weight: 4, opacity: 0.95, dashArray: r.method === "roads" ? null : "8 6" })
      .bindTooltip(`${d.level === "mv" ? `${d.circuits} × MV cable` : "110 kV cable"} · ${r.km.toFixed(2)} km`, { sticky: true }).addTo(g);
    for (let i = 1; i < r.path.length - 1; i += Math.max(1, Math.floor(r.path.length / 12))) L.circleMarker(r.path[i], { radius: 3, color: "#fff", weight: 1.5, fillColor: color, fillOpacity: 1 }).addTo(g);
    for (const c of r.cross.points || []) L.circleMarker(c, { radius: 6, color: "#fff", weight: 2, fillColor: "#e5484d", fillOpacity: 1 }).bindTooltip("Trenchless crossing (HDD)").addTo(g);
    const icon = (txt, bg) => L.divIcon({ className: "", iconSize: [0, 0], html: `<div style="transform:translate(-50%,-50%);background:${bg};color:#fff;font:600 11px system-ui,sans-serif;padding:3px 7px;border-radius:7px;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.35)">${txt}</div>` });
    L.marker(r.path[0], { icon: icon(d.level === "mv" ? "MV station" : `110/MV substation · ${d.transformers}×63 MVA`, "#e8930c"), interactive: false }).addTo(g);
    L.marker(r.path[r.path.length - 1], { icon: icon(`${d.level === "mv" ? "MV bay" : "110 kV bay"} · ${esc(stName)}`, "#0d1520"), interactive: false }).addTo(g);
    map.fitBounds(L.latLngBounds(r.path).pad(0.15), { maxZoom: 16, animate: false });
    return g;
  }

  root.GridConnect = { CONNECT, route, design, plan, html, draw, pln };
})(typeof window !== "undefined" ? window : globalThis);
