def ingest_passing(js):
        rs = rows(js)
        wrote = 0
        for r in rs:
            pid = r.get("PLAYER_ID")
            if pid is None or pid not in players:
                continue
            ast = num(r.get("AST"))
            apc = num(r.get("AST_POINTS_CREATED"))
            if apc is None:
                apc = num(r.get("AST_PTS_CREATED"))  # fallback field name
            if not ast or ast <= 0 or apc is None:
                continue
            ppa = apc / ast
            a3 = ppa - 2.0
            if a3 < 0:
                a3 = 0.0
            if a3 > 1:
                a3 = 1.0
            players[pid]["astTo3"] = round(a3, 3)
            players[pid]["astTo2"] = round(1.0 - a3, 3)
            wrote += 1
        # diagnostic: if nothing was written, record what the endpoint actually returned
        # (rows=0 -> WNBA has no passing tracking; rows>0 -> a field-name mismatch, keys shown)
        if wrote == 0:
            keys = sorted(rs[0].keys())[:45] if rs else []
            errors["passing_diag"] = "rows=%d wrote=0 keys=%s" % (len(rs), ",".join(keys))
