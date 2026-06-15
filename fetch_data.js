/*
 * fetch_data.js  —  nightly "Data Factory" for the Matchup-Weighted CUSH HR model.
 *
 * Dependency-free: uses Node 18+ global fetch + built-in modules only.
 * No npm install required. GitHub Actions runs `node fetch_data.js`, which
 * writes hr_matrix.json to the repo root for the app to fetch via
 * https://raw.githubusercontent.com/<you>/<repo>/main/hr_matrix.json
 */

const fs = require('fs');

const SEASON = Number(process.env.SEASON) || new Date().getFullYear();
const UA = 'Mozilla/5.0 (compatible; CushDataFactory/1.0; +https://cushplayerprops.win)';
const SAVANT = 'https://baseballsavant.mlb.com';
const MLB = 'https://statsapi.mlb.com/api/v1';
const CONCURRENCY = 4;        // pitchers fetched in parallel per batch
const BATCH_PAUSE_MS = 800;   // gentle pause between batches (rate-limit safety)

// ---------- small helpers ----------
function etDate(offsetDays) {
  // YYYY-MM-DD in America/New_York (MLB schedule dates are local game dates)
  const d = new Date(Date.now() + offsetDays * 86400000);
  const p = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit'
  }).formatToParts(d);
  const get = (t) => p.find((x) => x.type === t).value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}

async function getJSON(url) {
  const r = await fetch(url, { headers: { 'User-Agent': UA, Accept: 'application/json' } });
  if (!r.ok) throw new Error('HTTP ' + r.status + ' on ' + url);
  return r.json();
}
async function getText(url) {
  const r = await fetch(url, { headers: { 'User-Agent': UA } });
  if (!r.ok) throw new Error('HTTP ' + r.status + ' on ' + url);
  return r.text();
}

// minimal CSV parser (handles quoted fields with embedded commas)
function parseCSV(text) {
  const lines = text.split(/\r?\n/);
  if (!lines.length) return [];
  const headers = splitCSVLine(lines[0]);
  const out = [];
  for (let i = 1; i < lines.length; i++) {
    if (!lines[i]) continue;
    const cells = splitCSVLine(lines[i]);
    const row = {};
    for (let j = 0; j < headers.length; j++) row[headers[j]] = cells[j];
    out.push(row);
  }
  return out;
}
function splitCSVLine(line) {
  const res = []; let cur = ''; let q = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (q) {
      if (c === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else q = false; }
      else cur += c;
    } else {
      if (c === '"') q = true;
      else if (c === ',') { res.push(cur); cur = ''; }
      else cur += c;
    }
  }
  res.push(cur);
  return res;
}

// Statcast classifies contact quality in launch_speed_angle; code 6 == Barrel.
function isBarrel(row) {
  return String(row.launch_speed_angle || '').trim() === '6';
}
// A batted-ball event = Statcast tracked a launch_speed (the barrel% denominator).
function isBattedBall(row) {
  const ev = row.launch_speed;
  return ev != null && ev !== '' && !isNaN(parseFloat(ev));
}

function detailsURL(pid) {
  // Canonical long-form Savant search CSV with a single pitcher, regular season.
  return `${SAVANT}/statcast_search/csv?all=true&hfPT=&hfAB=&hfGT=R%7C&hfPR=&hfZ=` +
    `&hfStadium=&hfBBL=&hfNewZones=&hfPull=&hfC=&hfSea=${SEASON}%7C&hfSit=` +
    `&player_type=pitcher&hfOuts=&hfOpponent=&pitcher_throws=&batter_stands=&hfSA=` +
    `&game_date_gt=&game_date_lt=&hfMo=&hfTeam=&home_road=&hfRO=&position=` +
    `&hfInfield=&hfOutfield=&hfInn=&hfBBT=&hfFlag=&metric_1=&group_by=name` +
    `&min_pitches=0&min_results=0&min_pas=0&sort_col=pitches` +
    `&player_event_sort=api_p_release_speed&sort_order=desc&type=details&pitchers_lookup%5B%5D=${pid}`;
}

// ---------- data pulls ----------
async function getStarters() {
  const dates = [etDate(0), etDate(1)]; // today + tomorrow (ET) to catch posted probables
  const ids = new Map(); // id -> { name, throws }
  for (const d of dates) {
    try {
      const data = await getJSON(`${MLB}/schedule?sportId=1&date=${d}&hydrate=probablePitcher`);
      for (const day of data.dates || []) {
        for (const g of day.games || []) {
          for (const side of ['home', 'away']) {
            const pp = g.teams && g.teams[side] && g.teams[side].probablePitcher;
            if (pp && pp.id && !ids.has(pp.id)) {
              ids.set(pp.id, { name: pp.fullName || String(pp.id), throws: null });
            }
          }
        }
      }
    } catch (e) { console.error('schedule ' + d + ' failed:', e.message); }
  }
  return ids;
}

async function enrichThrows(ids) {
  const list = [...ids.keys()];
  if (!list.length) return;
  try {
    const data = await getJSON(`${MLB}/people?personIds=${list.join(',')}`);
    for (const p of data.people || []) {
      if (ids.has(p.id) && p.pitchHand && p.pitchHand.code) ids.get(p.id).throws = p.pitchHand.code;
    }
  } catch (e) { console.error('throws enrich failed:', e.message); }
}

// ---------- plate discipline (chase / whiff / zone-contact) from pitch-level rows ----------
// Savant 'description' values that count as a swing, and the subset that are whiffs (misses).
const SWING_DESC = { swinging_strike: 1, swinging_strike_blocked: 1, foul: 1, foul_tip: 1, hit_into_play: 1, foul_bunt: 1, missed_bunt: 1, bunt_foul_tip: 1 };
const WHIFF_DESC = { swinging_strike: 1, swinging_strike_blocked: 1, missed_bunt: 1 };
function newDisc() { return { pit: 0, oz: 0, sw: 0, izSw: 0, ozSw: 0, wh: 0, izWh: 0, ozWh: 0 }; }
function tallyDisc(d, inZone, outZone, isSwing, isWhiff) {
  d.pit++; if (outZone) d.oz++;
  if (isSwing) {
    d.sw++; if (inZone) d.izSw++; if (outZone) d.ozSw++;
    if (isWhiff) { d.wh++; if (inZone) d.izWh++; if (outZone) d.ozWh++; }
  }
}
function discPct(d) {
  if (!d || !d.pit) return null;
  return {
    n: d.pit,                                                                  // pitches in this cell
    whiff: d.sw ? +(((d.wh) / d.sw) * 100).toFixed(1) : null,                   // whiffs per swing
    chase: d.oz ? +((d.ozSw / d.oz) * 100).toFixed(1) : null,                   // O-Swing% (chase)
    oCon: d.ozSw ? +(((d.ozSw - d.ozWh) / d.ozSw) * 100).toFixed(1) : null,     // O-Contact% (chase contact)
    zCon: d.izSw ? +(((d.izSw - d.izWh) / d.izSw) * 100).toFixed(1) : null      // Z-Contact% (zone contact)
  };
}

async function buildPitcher(pid, meta) {
  const csv = await getText(detailsURL(pid));
  if (!csv || csv.indexOf('pitch_type') < 0) throw new Error('no CSV / possibly rate-limited');
  const rows = parseCSV(csv);

  const pitchCount = {}; let totalPitches = 0;
  const cells = {}; // pt -> hand -> { bbe, barrels }
  const disc = { L: newDisc(), R: newDisc(), ALL: newDisc() }; // plate discipline vs LHB/RHB/all

  for (const r of rows) {
    const pt = (r.pitch_type ||
