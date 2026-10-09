#!/usr/bin/env python3
# Cush Player Props - NFL Monte Carlo team profiles (drive-level sim inputs)
# Source: nflverse pbp (free GitHub release). Output: nfl_mc.json
# Per team: offensive + defensive efficiency vectors the drive engine reads.
import os, sys, json, csv, io, datetime, urllib.request
REL="https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv"
OUT="nfl_mc.json"; STYPE=os.environ.get("NFL_MC_SEASON_TYPE") or "REG"; EXP=20
def num(x):
    try:
        if x is None or x=="" or x=="NA": return None
        return float(x)
    except: return None
def fetch(url):
    req=urllib.request.Request(url,headers={"User-Agent":"cush-nfl-mc/1.0"})
    with urllib.request.urlopen(req,timeout=240) as r: return r.read()
def http_ok(url):
    try:
        req=urllib.request.Request(url,method="HEAD",headers={"User-Agent":"cush-nfl-mc/1.0"})
        with urllib.request.urlopen(req,timeout=30) as r: return r.status==200
    except: return False
def pick_season():
    f=os.environ.get("NFL_MC_SEASON")
    if f: return int(f)
    y=datetime.date.today().year
    for s in range(y+1,y-6,-1):
        if http_ok(REL.format(season=s)): return s
    raise SystemExit("no pbp file")
def agg():
    season=pick_season(); url=REL.format(season=season)
    sys.stderr.write("season %d %s\n"%(season,url))
    rows=list(csv.DictReader(io.StringIO(fetch(url).decode("utf-8","replace"))))
    rows=[r for r in rows if (r.get("season_type") or "REG")==STYPE]
    # accumulators keyed by team for offense (posteam) and defense (defteam)
    def mk(): return {"ed_epa_sum":0.0,"ed_n":0,"succ_sum":0.0,"succ_n":0,"exp_n":0,"pr_n":0,
                      "drb":0.0,"sacks":0.0,"gv":0,"drives":set(),"games":set(),
                      "rz_dr":0,"rz_td":0}
    off={}; dfn={}
    # drive-level tracking: (team,game,drive)-> {reached_rz, td}
    odr={}; ddr={}
    for r in rows:
        po=r.get("posteam"); de=r.get("defteam")
        if not po or not de: continue
        gid=r.get("game_id"); fd=r.get("fixed_drive")
        isp=num(r.get("pass")); isr=num(r.get("rush")); epa=num(r.get("epa"))
        down=num(r.get("down")); yg=num(r.get("yards_gained")); succ=num(r.get("success"))
        drb=num(r.get("qb_dropback")); sack=num(r.get("sack"))
        intc=num(r.get("interception")) or 0; fl=num(r.get("fumble_lost")) or 0
        ptd=num(r.get("pass_touchdown")) or 0; rtd=num(r.get("rush_touchdown")) or 0
        yl=num(r.get("yardline_100"))
        for side,T,acc in (("off",po,off),("def",de,dfn)):
            a=acc.setdefault(T,mk())
            if (isp==1 or isr==1) and epa is not None:
                a["pr_n"]+=1
                if succ is not None: a["succ_sum"]+=succ; a["succ_n"]+=1
                if yg is not None and yg>=EXP: a["exp_n"]+=1
                if down in (1.0,2.0): a["ed_epa_sum"]+=epa; a["ed_n"]+=1
            if drb==1: a["drb"]+=1
            if sack==1: a["sacks"]+=1
            if gid: a["games"].add(gid)
        # offense giveaways / drives
        ao=off[po]
        if intc or fl: ao["gv"]+=int(intc+fl)
        if gid and fd: ao["drives"].add((gid,fd))
        # defense takeaways (= opponent giveaways) / opp drives faced
        ad=dfn[de]
        if intc or fl: ad["gv"]+=int(intc+fl)   # takeaways for defense
        if gid and fd: ad["drives"].add((gid,fd))
        # rz drive tracking (offense perspective and defense-allowed)
        if gid and fd and yl is not None:
            k=(po,gid,fd)
            o=odr.setdefault(k,{"rz":False,"td":False}); 
            if yl<=20: o["rz"]=True
            if ptd or rtd: o["td"]=True
            k2=(de,gid,fd)
            dd=ddr.setdefault(k2,{"rz":False,"td":False})
            if yl<=20: dd["rz"]=True
            if ptd or rtd: dd["td"]=True
    # fold rz drive dicts into team rz counts
    for (T,gid,fd),v in odr.items():
        if v["rz"]: off[T]["rz_dr"]+=1;  off[T]["rz_td"]+= (1 if v["td"] else 0)
    for (T,gid,fd),v in ddr.items():
        if v["rz"]: dfn[T]["rz_dr"]+=1;  dfn[T]["rz_td"]+= (1 if v["td"] else 0)
    teams=sorted(set(off)&set(dfn))
    prof={}
    for T in teams:
        o=off[T]; d=dfn[T]
        og=max(1,len(o["games"])); odrv=max(1,len(o["drives"])); ddrv=max(1,len(d["drives"]))
        prof[T]={
            "ed_epa": round(o["ed_epa_sum"]/max(1,o["ed_n"]),4),
            "success": round(o["succ_sum"]/max(1,o["succ_n"]),4),
            "explosive": round(o["exp_n"]/max(1,o["pr_n"]),4),
            "sack_rate": round(o["sacks"]/max(1,o["drb"]),4),
            "giveaway_pd": round(o["gv"]/odrv,4),
            "rz_td": round(o["rz_td"]/max(1,o["rz_dr"]),4) if o["rz_dr"] else 0.60,
            "drives_pg": round(odrv/og,2),
            "def_ed_epa": round(d["ed_epa_sum"]/max(1,d["ed_n"]),4),
            "def_success": round(d["succ_sum"]/max(1,d["succ_n"]),4),
            "def_explosive": round(d["exp_n"]/max(1,d["pr_n"]),4),
            "def_sack_rate": round(d["sacks"]/max(1,d["drb"]),4),
            "def_takeaway_pd": round(d["gv"]/ddrv,4),
            "def_rz_td": round(d["rz_td"]/max(1,d["rz_dr"]),4) if d["rz_dr"] else 0.60,
        }
    # league means (average of team values, so edges center ~0)
    keys=list(next(iter(prof.values())).keys())
    lg={k: round(sum(prof[t][k] for t in teams)/len(teams),4) for k in keys}
    league={
        "ed_epa": lg["ed_epa"], "success": lg["success"], "explosive": lg["explosive"],
        "sack_rate": lg["sack_rate"], "giveaway_pd": lg["giveaway_pd"], "takeaway_pd": lg["def_takeaway_pd"],
        "rz_td": lg["rz_td"], "drives_pg": lg["drives_pg"],
        "base_td":0.250,"base_fg":0.173,"base_to":0.115,"base_punt":0.378,
        "start_fp_mean":73.0,"start_fp_sd":10.0,"fg_d50":60.0,"fg_slope":6.2,
    }
    through=0
    for r in rows:
        try: through=max(through,int(float(r.get("week") or 0)))
        except: pass
    return {"season":season,"seasonType":STYPE,
            "updated":datetime.datetime.utcnow().replace(microsecond=0).isoformat()+"Z",
            "throughWeek":through,"explosiveYds":EXP,"teams":prof,"league":league}
if __name__=="__main__":
    out=agg()
    open(OUT,"w").write(json.dumps(out,separators=(",",":")))
    sys.stderr.write("wrote %s teams=%d throughWeek=%d\n"%(OUT,len(out["teams"]),out["throughWeek"]))
    L=out["league"]; sys.stderr.write("league ed_epa=%.4f succ=%.4f exp=%.4f sack=%.4f giveaway=%.4f rz=%.3f dpg=%.2f\n"%(L["ed_epa"],L["success"],L["explosive"],L["sack_rate"],L["giveaway_pd"],L["rz_td"],L["drives_pg"]))
