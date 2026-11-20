"""
DC power flow on the network.json model: PTDF, LODF and N-0 / N-1 screening.

Rows (branches whose flow is computed) are the monitored Polish 220/400 kV
lines and 400/220 kV transformers, the tie lines and the contingencies; the
110 kV grid and the neighbours' lines carry flow but are neither monitored
nor tripped.  Monitored and tripped lines are split into their circuits.

Phase shifters: the German tie corridors (Mikułowa - Hagenwerder with PSE's
PSTs at Mikułowa, Krajnik - Vierraden with 50Hertz's at Vierraden) are
flow-controlled.  Their base-case flow is the network's, but after another
element trips they keep it (the LODFs are computed with those branches out
and their flows held as injections).

Ratings: s_nom (at 30 degrees C air) times sqrt((Tc - Ta) / (Tc - 30)) for
the hour's air temperature Ta at the line (ERA5), Tc the conductor design
temperature (80, or 60 for lines the plan upgrades), within 0.6-1.3.

Outages: `Model.state(out)` gives PTDF and LODF with a set of branches out of
service (ENTSO-E's planned and forced outages for that hour).
"""

import json
import math
import os

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spl

PST_CORRIDORS = (("mikulowa", "DE"), ("krajnik", "DE"))


def fold(s):
    import unicodedata
    return unicodedata.normalize("NFKD", str(s or "").replace("ł", "l").replace("Ł", "L")).encode("ascii", "ignore").decode().lower()


class Model:
    def __init__(self, work, pst="fixed"):
        net = json.load(open(os.path.join(work, "out", "network.json"), encoding="utf-8"))
        self.net = net
        self.buses = net["buses"]
        self.bidx = {b["id"]: i for i, b in enumerate(self.buses)}
        br = []
        for b in net["branches"]:
            split = b["kind"] == "line" and (b.get("monitored") or b["tie"])
            n = max(1, int(b["circuits"])) if split else 1
            for c in range(n):
                br.append(dict(b, circuit=c + 1, of=n, x_pu=b["x_pu"] * n, s_nom=b["s_nom"] / n))
        self.branches = br
        nb, nl = len(self.buses), len(br)
        self.nb, self.nl = nb, nl
        f = np.array([self.bidx[b["bus0"]] for b in br])
        t = np.array([self.bidx[b["bus1"]] for b in br])
        self.f, self.t = f, t
        self.b = 1.0 / np.array([b["x_pu"] for b in br])
        self.mon = np.array([bool(b.get("monitored")) or bool(b["tie"]) for b in br])
        self.is_line = np.array([b["kind"] == "line" for b in br])
        self.tie = np.array([bool(b["tie"]) for b in br])
        self.s = np.array([b["s_nom"] for b in br])
        # contingencies: Polish 220/400 kV circuits, ties and 400/220 kV transformers
        self.cont_all = np.where(self.mon)[0]
        def is_pst(b):
            ends = [self.buses[self.bidx[b["bus0"]]], self.buses[self.bidx[b["bus1"]]]]
            pl = [e for e in ends if e["country"] == "PL"]
            other = [e for e in ends if e["country"] != "PL"]
            return (pst == "fixed" and b["tie"] and len(pl) == 1 and len(other) == 1
                    and any(fold(pl[0]["name"]).startswith(p) and other[0]["country"] == c for p, c in PST_CORRIDORS))
        self.pst = np.array([is_pst(b) for b in br])
        self.rows = np.where(self.mon)[0]  # every row we need is monitored (ties included)
        self.ref = next(i for i, b in enumerate(self.buses) if b["country"] == "PL" and b["kv"] == 400)
        self.keep = np.array([i for i in range(nb) if i != self.ref])
        self._cache = {}

    # ---- matrices for one topology
    def _ptdf(self, out):
        """PTDF rows [len(self.rows), nb] with the branches in `out` removed."""
        on = np.ones(self.nl, bool)
        on[list(out)] = False
        b = np.where(on, self.b, 0.0)
        C = sp.csr_matrix((np.r_[np.ones(self.nl), -np.ones(self.nl)], (np.r_[np.arange(self.nl), np.arange(self.nl)], np.r_[self.f, self.t])),
                          shape=(self.nl, self.nb))
        B = (C.T @ sp.diags(b) @ C).tocsc()
        k = self.keep
        # a negligible shunt keeps islands (a stub left when the PST corridors are taken out) solvable
        lu = spl.splu((B[k][:, k] + sp.identity(len(k)) * 1e-6).tocsc())
        R = (sp.diags(b[self.rows]) @ C[self.rows]).toarray()  # [rows, nb]
        X = np.zeros((len(self.rows), self.nb))
        X[:, k] = lu.solve(np.ascontiguousarray(R[:, k].T)).T
        return X

    def _outaged(self, base, out):
        """PTDF rows with the branches in `out` (all rows) removed: a low-rank update of `base`.
        Outages that would island part of the grid are left in service."""
        rpos = {r: i for i, r in enumerate(self.rows)}
        M = [m for m in out if m in rpos]
        while M:
            tr = base[:, self.f[M]] - base[:, self.t[M]]  # [rows, k]
            A = np.eye(len(M)) - tr[[rpos[m] for m in M], :]
            if np.linalg.cond(A) < 1e8:
                P = base + tr @ np.linalg.solve(A, base[[rpos[m] for m in M], :])
                P[[rpos[m] for m in M], :] = 0.0  # the update formula holds for the other branches; these carry nothing
                return P, M
            M = M[:-1]
        return base, []

    def state(self, out=frozenset()):
        """(ptdf_rows, lodf[rows, cont], cont, bridge, applied outages) for a topology; cached."""
        out = frozenset(out)
        if out in self._cache:
            return self._cache[out]
        if "base" not in self._cache:
            self._cache["base"] = self._ptdf(set())
            self._cache["base_pst"] = self._ptdf(set(np.where(self.pst)[0])) if self.pst.any() else self._cache["base"]
        P, applied = self._outaged(self._cache["base"], sorted(out))
        out = frozenset(applied)
        rpos = {r: i for i, r in enumerate(self.rows)}
        cont = np.array([k for k in self.cont_all if k not in out])
        # LODFs on the network without the flow-controlled PST branches
        Pn = self._outaged(self._cache["base_pst"], sorted(out - set(np.where(self.pst)[0])))[0] if self.pst.any() else P
        ci = np.array([rpos[k] for k in cont])
        tr = Pn[:, self.f[cont]] - Pn[:, self.t[cont]]  # [rows, cont]: flow on row per MW sent across k
        denom = 1.0 - tr[ci, np.arange(len(cont))]
        bridge = np.abs(denom) < 1e-6
        lodf = tr / np.where(bridge, 1.0, denom)[None, :]
        lodf[:, bridge] = 0.0
        lodf[ci, np.arange(len(cont))] = -1.0
        pst_rows = np.array([rpos[k] for k in np.where(self.pst)[0]], int)
        lodf[pst_rows, :] = 0.0       # a PST corridor keeps its flow
        lodf[:, np.isin(cont, np.where(self.pst)[0])] = 0.0  # tripping one is not screened here
        lodf[pst_rows[:, None], np.where(np.isin(cont, np.where(self.pst)[0]))[0][None, :]] = 0.0
        res = (P, lodf.astype("float32"), cont, bridge, out)
        if len(self._cache) > 60:
            for k in [k for k in self._cache if k not in ("base", "base_pst")][:30]:
                del self._cache[k]
        self._cache[out] = res
        return res

    # ---- ratings
    def ratings(self, work, hours):
        """[T, rows] ratings in MVA from each line's air temperature."""
        w = np.load(os.path.join(work, "cache", "weather_pl.npz"))
        import pandas as pd
        pos = pd.Index(pd.to_datetime(w["time"], unit="s", utc=True)).get_indexer(hours)
        temp = w["temp"][pos]  # [T, cell]
        t_ref = self.net.get("t_ref", 30.0)
        out = np.zeros((len(hours), len(self.rows)), dtype="float32")
        for j, r in enumerate(self.rows):
            b = self.branches[r]
            b0, b1 = self.buses[self.f[r]], self.buses[self.t[r]]
            pts = [(x["lat"], x["lon"]) for x in (b0, b1) if x["lat"] is not None]
            lat, lon = np.mean([p[0] for p in pts]), np.mean([p[1] for p in pts])
            cell = int(np.argmin((w["y"] - lat) ** 2 + ((w["x"] - lon) * math.cos(math.radians(lat))) ** 2))
            tc = b.get("t_design", 80.0)
            if b["kind"] == "trafo":
                out[:, j] = self.s[r]
                continue
            fac = np.sqrt(np.clip(tc - temp[:, cell], 1.0, None) / (tc - t_ref))
            out[:, j] = self.s[r] * np.clip(fac, 0.6, 1.3)
        return out

    # ---- screening
    def screen(self, F, R, lodf, cont, chunk=96, subset=None):
        """F [T, rows] base flows, R [T, rows] ratings: worst post-contingency loading and its contingency.
        With `subset` (row positions), only those rows are screened (the others come back 0)."""
        T, L = F.shape
        rpos = {r: i for i, r in enumerate(self.rows)}
        ci = np.array([rpos[k] for k in cont])
        mon = np.arange(L) if subset is None else np.asarray(subset)
        lo = lodf[mon]
        own = np.array([np.where(ci == j)[0][0] if j in set(ci) else -1 for j in mon])
        worst = np.zeros((T, L), dtype="float32")
        arg = np.zeros((T, L), dtype="int32")
        for t0 in range(0, T, chunk):
            f = F[t0:t0 + chunk].astype("float32")
            post = f[:, mon, None] + lo[None, :, :] * f[:, ci][:, None, :]
            has = own >= 0
            post[:, np.where(has)[0], own[has]] = 0.0
            a = np.abs(post)
            k = a.argmax(axis=2)
            worst[t0:t0 + chunk, mon] = np.take_along_axis(a, k[:, :, None], 2)[:, :, 0] / R[t0:t0 + chunk][:, mon]
            arg[t0:t0 + chunk, mon] = cont[k]
        return worst, arg
