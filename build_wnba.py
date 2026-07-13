# Passing tracking -> how a PLAYER'S OWN ASSISTS split between 3s and 2s.
    # Every assist is on a made field goal (2 or 3; free throws are never assisted),
    # so average points per assist gives the exact mix:
    #   ppa = AST_PTS_CREATED / AST  (2..3);  astTo3 = ppa - 2;  astTo2 = 1 - astTo3
    def ptparams(measure):
        return {
            "College": "", "Conference": "", "Country": "", "DateFrom": "", "DateTo": "",
            "Division": "", "DraftPick": "", "DraftYear": "", "GameScope": "", "Height": "",
            "LastNGames": "0", "LeagueID": LEAGUE, "Location": "", "Month": "0",
            "OpponentTeamID": "0", "Outcome": "", "PORound": "0", "PerMode": "PerGame",
            "PlayerExperience": "", "PlayerOrTeam": "Player", "PlayerPosition": "",
            "PtMeasureType": measure, "Season": SEASON, "SeasonSegment": "",
            "SeasonType": "Regular Season", "StarterBench": "", "TeamID": "0",
            "VsConference": "", "VsDivision": "", "Weight": "",
        }

    def ingest_passing(js):
        for r in rows(js):
            pid = r.get("PLAYER_ID")
            if pid is None or pid not in players:
                continue
            ast = num(r.get("AST"))
            apc = num(r.get("AST_PTS_CREATED"))
            if not ast or ast <= 0 or apc is None:
                continue
            ppa = apc / ast
            a3 = ppa - 2.0
            if a3 < 0: a3 = 0.0
            if a3 > 1: a3 = 1.0
            players[pid]["astTo3"] = round(a3, 3)
            players[pid]["astTo2"] = round(1.0 - a3, 3)

    try:
        ingest_passing(get("/leaguedashptstats", ptparams("Passing")))
    except Exception as e:
        errors["passing"] = str(e)
