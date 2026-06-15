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
    const pt = (r.pitch_type || '').trim();
    if (!pt || pt === 'null') continue;
    pitchCount[pt] = (pitchCount[pt] || 0) + 1;
    totalPitches++;

    const standRaw = (r.stand || '').trim().toUpperCase();
    const hand = standRaw === 'L' ? 'L' : standRaw === 'R' ? 'R' : null;

    // plate discipline — every pitch counts (not just batted balls)
    const desc = (r.description || '').trim();
    const zoneNum = parseInt(r.zone, 10);
    const inZone = zoneNum >= 1 && zoneNum <= 9;     // Statcast zones 1-9 = strike zone
    const outZone = zoneNum >= 11 && zoneNum <= 14;  // zones 11-14 = out of zone (chase territory)
    const isSwing = SWING_DESC[desc] === 1;
    const isWhiff = WHIFF_DESC[desc] === 1;
    tallyDisc(disc.ALL, inZone, outZone, isSwing, isWhiff);
    if (hand) tallyDisc(disc[hand], inZone, outZone, isSwing, isWhiff);

    // barrels-allowed matrix (batted balls only)
    if (!isBattedBall(r)) continue;
    const barrel = isBarrel(r) ? 1 : 0;
    if (!cells[pt]) cells[pt] = {};
    if (hand) {
      if (!cells[pt][hand]) cells[pt][hand] = { bbe: 0, barrels: 0 };
      cells[pt][hand].bbe++; cells[pt][hand].barrels += barrel;
    }
    if (!cells[pt].ALL) cells[pt].ALL = { bbe: 0, barrels: 0 };
    cells[pt].ALL.bbe++; cells[pt].ALL.barrels += barrel;
  }

  if (totalPitches === 0) return null;
  if (totalPitches > 6000) throw new Error('too many pitches (' + totalPitches + ') — player filter likely failed');

  const arsenal = {};
  for (const pt in pitchCount) arsenal[pt] = +(pitchCount[pt] / totalPitches).toFixed(3);

  const allowed = {};
  for (const pt in cells) {
    allowed[pt] = {};
    for (const hand of ['L', 'R', 'ALL']) {
      const c = cells[pt][hand];
      if (c && c.bbe > 0) allowed[pt][hand] = { brlPct: +((c.barrels / c.bbe) * 100).toFixed(1), n: c.bbe };
    }
  }

  return { name: meta.name, throws: meta.throws || null, arsenal, allowed, disc: { L: discPct(disc.L), R: discPct(disc.R), ALL: discPct(disc.ALL) } };
}

// MLB innings-pitched string ("412.1" == 412 + 1/3) -> decimal
function parseIP(ipStr){
  if(ipStr==null)return 0;
  var s=String(ipStr), dot=s.indexOf('.');
  if(dot<0)return Number(s)||0;
  var whole=Number(s.slice(0,dot))||0, frac=s.slice(dot+1);
  return whole + (frac==='1'?1/3:frac==='2'?2/3:0);
}

// Team bullpen (relief-only) HR/9 + batters faced, season, via statsapi role split.
async function getBullpens(){
  var out={};
  var teams;
  try{ teams=(await getJSON(`${MLB}/teams?sportId=1&season=${SEASON}`)).teams||[]; }
  catch(e){ console.error('teams list failed:', e.message); return out; }
  for(const t of teams){
    try{
      var url=`${MLB}/teams/${t.id}/stats?stats=statSplits&group=pitching&season=${SEASON}&gameType=R&sitCodes=rp`;
      var d=await getJSON(url);
      var s=d.stats&&d.stats[0]&&d.stats[0].splits&&d.stats[0].splits[0]&&d.stats[0].splits[0].stat;
      if(!s)continue;
      var hr=Number(s.homeRuns)||0, ip=parseIP(s.inningsPitched), bf=Number(s.battersFaced)||0;
      if(ip>0) out[t.id]={team:t.abbreviation||t.teamName||String(t.id),hr9:+((hr/ip)*9).toFixed(3),bf:bf,ip:+ip.toFixed(1)};
    }catch(e){ console.error('bullpen', t.id, 'failed:', e.message); }
    await new Promise(function(r){setTimeout(r,60);});
  }
  return out;
}

// ---------- main ----------
async function main() {
  console.log('Data Factory start — season', SEASON);
  const ids = await getStarters();
  await enrichThrows(ids);
  console.log('Probable starters found:', ids.size);

  const pitchers = {};
  const entries = [...ids.entries()];
  for (let i = 0; i < entries.length; i += CONCURRENCY) {
    const batch = entries.slice(i, i + CONCURRENCY);
    await Promise.all(batch.map(async ([pid, meta]) => {
      try {
        const rec = await buildPitcher(pid, meta);
        if (rec) { pitchers[pid] = rec; console.log('  ok   ', pid, meta.name); }
        else console.log('  empty', pid, meta.name);
      } catch (e) { console.error('  fail ', pid, meta.name, '-', e.message); }
    }));
    if (i + CONCURRENCY < entries.length) await new Promise((r) => setTimeout(r, BATCH_PAUSE_MS));
  }

  const count = Object.keys(pitchers).length;
  if (count === 0) {
    // Do NOT overwrite the last good file with an empty one (off-day / total failure).
    console.error('No pitchers built — leaving existing hr_matrix.json untouched.');
    process.exit(0);
  }

  console.log('Fetching team bullpen stats...');
  const bullpens = await getBullpens();
  console.log('Bullpens built for', Object.keys(bullpens).length, 'teams.');

  const out = { season: SEASON, generatedAt: new Date().toISOString(), pitchers, bullpens };
  fs.writeFileSync('hr_matrix.json', JSON.stringify(out));
  console.log('Wrote hr_matrix.json with', count, 'pitchers.');
}

main().catch((e) => { console.error('FATAL', e); process.exit(1); });
