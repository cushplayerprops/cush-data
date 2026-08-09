// pull_wnba_games.mjs
// Pulls the full WNBA regular-season game log (all completed games to date)
// from ESPN's free public API and writes wnba_team_games.json.
// No dependencies — uses Node 18+ built-in fetch. Runs on GitHub Actions.

const SEASON = Number(process.env.WNBA_SEASON) || 2025;
const BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba";
const OUT = "wnba_team_games.json";

async function getJSON(url, tries = 3) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url, { headers: { "User-Agent": "cush-wnba/1.0" } });
      if (!r.ok) throw new Error("HTTP " + r.status);
      return await r.json();
    } catch (e) {
      if (i === tries - 1) throw e;
      await new Promise(res => setTimeout(res, 1200 * (i + 1)));
    }
  }
}

function num(v) {
  if (v == null) return null;
  if (typeof v === "number") return v;
  if (typeof v === "object") return num(v.value != null ? v.value : v.displayValue);
  const n = parseInt(String(v).replace(/[^\d-]/g, ""), 10);
  return isNaN(n) ? null : n;
}

async function teamList() {
  const j = await getJSON(`${BASE}/teams`);
  const arr = (((j.sports || [])[0] || {}).leagues || [])[0]?.teams || [];
  return arr.map(t => t.team).filter(Boolean).map(t => ({
    id: String(t.id),
    abbr: t.abbreviation || (t.shortDisplayName || "").toUpperCase(),
    name: t.displayName || t.name
  }));
}

async function teamGames(team) {
  const url = `${BASE}/teams/${team.id}/schedule?season=${SEASON}&seasontype=2`;
  const j = await getJSON(url);
  const events = j.events || (j.team && j.team.events) || [];
  const games = [];
  for (const ev of events) {
    const comp = (ev.competitions || [])[0];
    if (!comp) continue;
    const done = comp.status?.type?.completed ?? ev.status?.type?.completed ?? false;
    if (!done) continue;
    const cs = comp.competitors || [];
    const me = cs.find(c => String(c.team?.id) === team.id) || cs.find(c => c.homeAway);
    const opp = cs.find(c => c !== me);
    if (!me || !opp) continue;
    const pf = num(me.score), pa = num(opp.score);
    if (pf == null || pa == null) continue;
    games.push({
      date: (ev.date || comp.date || "").slice(0, 10),
      opp: opp.team?.abbreviation || opp.team?.shortDisplayName || "?",
      home: (me.homeAway || "") === "home",
      pf, pa,
      win: me.winner === true ? true : (opp.winner === true ? false : pf > pa)
    });
  }
  games.sort((a, b) => a.date.localeCompare(b.date));
  return games;
}

(async () => {
  const teams = await teamList();
  if (!teams.length) throw new Error("no teams returned from ESPN");
  const out = { season: SEASON, updated: new Date().toISOString().slice(0, 10), teams: {} };
  let total = 0;
  for (const t of teams) {
    try {
      const g = await teamGames(t);
      out.teams[t.abbr] = { name: t.name, abbr: t.abbr, games: g };
      total += g.length;
      console.log(`${t.abbr.padEnd(4)} ${t.name.padEnd(24)} ${g.length} games`);
    } catch (e) {
      console.error(`FAILED ${t.abbr}: ${e.message}`);
      out.teams[t.abbr] = { name: t.name, abbr: t.abbr, games: [] };
    }
    await new Promise(res => setTimeout(res, 400)); // be polite
  }
  const fs = await import("node:fs");
  fs.writeFileSync(OUT, JSON.stringify(out));
  console.log(`\nWrote ${OUT}: ${teams.length} teams, ${total} completed games (season ${SEASON}).`);
})().catch(e => { console.error("FATAL", e); process.exit(1); });
