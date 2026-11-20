"""
DC power flow on the network.json model: PTDF, LODF and N-0 / N-1 screening.

Each OSM line with n circuits becomes n parallel branches, so a single-circuit
outage of a double line is a contingency of its own.  Injections must sum to
zero every hour (inputs.py closes the balance), so the reference bus does not
matter.
"""

import json
import os

import numpy as np


class Model:
    def __init__(self, work):
        net = json.load(open(os.path.join(work, "out", "network.json"), encoding="utf-8"))
        self.buses = net["buses"]
        self.bidx = {b["id"]: i for i, b in enumerate(self.buses)}
        br = []
        for b in net["branches"]:
            n = max(1, int(b["circuits"])) if b["kind"] == "line" else 1
            for c in range(n):
                br.append(dict(b, circuit=c + 1, of=n, x_pu=b["x_pu"] * n, s_nom=b["s_nom"] / n))
        self.branches = br
        nb, nl = len(self.buses), len(br)
        C = np.zeros((nl, nb))
        for i, b in enumerate(br):
            C[i, self.bidx[b["bus0"]]] = 1
            C[i, self.bidx[b["bus1"]]] = -1
        bvec = 1.0 / np.array([b["x_pu"] for b in br])
        ref = next(i for i, b in enumerate(self.buses) if not b["boundary"])
        keep = [i for i in range(nb) if i != ref]
        Bbus = C.T @ (bvec[:, None] * C)
        X = np.zeros((nb, nb))
        X[np.ix_(keep, keep)] = np.linalg.inv(Bbus[np.ix_(keep, keep)])
        self.ptdf = (bvec[:, None] * C) @ X  # [branch, bus], MW per MW injected (withdrawn at ref)
        self.s = np.array([b["s_nom"] for b in br])
        # LODF[l, k]: share of branch k's pre-outage flow that moves onto l when k trips.
        transfer = self.ptdf @ C.T  # [l, k]: flow on l per MW sent from k's bus0 to bus1
        denom = 1.0 - np.diag(transfer)
        self.bridge = np.abs(denom) < 1e-6  # tripping it islands part of the grid: not screened
        lodf = transfer / np.where(self.bridge, 1.0, denom)[None, :]
        np.fill_diagonal(lodf, -1.0)
        lodf[:, self.bridge] = 0.0
        self.lodf = lodf
        self.line = np.array([b["kind"] == "line" for b in br])
        self.tie = np.array([bool(b["tie"]) for b in br])
        # Contingencies: every circuit and transformer whose loss does not island the grid.
        self.cont = np.where(~self.bridge)[0]

    def flows(self, P):
        """N-0 branch flows [T, branch] for injections P [T, bus]."""
        return P @ self.ptdf.T

    def screen(self, F, chunk=200):
        """Worst post-contingency loading per hour and branch, and the contingency causing it."""
        T, L = F.shape
        worst = np.zeros((T, L), dtype="float32")
        arg = np.zeros((T, L), dtype="int16")
        lo = self.lodf[:, self.cont].astype("float32")  # [L, K]
        inv_s = (1.0 / self.s).astype("float32")
        for t0 in range(0, T, chunk):
            f = F[t0:t0 + chunk].astype("float32")
            post = f[:, :, None] + lo[None, :, :] * f[:, self.cont][:, None, :]  # [t, l, k]
            # A branch is not monitored against its own outage.
            idx = np.arange(len(self.cont))
            post[:, self.cont, idx] = 0.0
            a = np.abs(post)
            k = a.argmax(axis=2)
            worst[t0:t0 + chunk] = np.take_along_axis(a, k[:, :, None], 2)[:, :, 0] * inv_s
            arg[t0:t0 + chunk] = self.cont[k]
        return worst, arg
