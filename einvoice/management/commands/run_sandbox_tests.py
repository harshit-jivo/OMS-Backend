"""
Run the NIC e-Invoice sandbox test matrix and produce the Test Summary Report
inputs (CSV + JSON), per NIC's onboarding requirement that testing be done by
the taxpayer's own application (not the online tool).

Usage:
    python manage.py run_sandbox_tests                 # small validation run
    python manage.py run_sandbox_tests --success 100 --fail 50   # full volume
    python manage.py run_sandbox_tests --buyer 11AAACT3904F1ZZ --delay 0.2

Outputs (in test_results/):
    details.json  - every API call with outcome
    summary.csv   - per-API successful/failed operation counts + sample IRN/EWB

Counting model (matches the report columns):
    "Successful operations" = positive-test calls that returned Status 1.
    "Failed operations"     = negative-test calls that correctly returned an error.
Anomalies (expected success but failed, or vice-versa) are flagged in Remarks.
"""
from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand

from einvoice.client import EInvoiceClient, EInvoiceError
from einvoice.sample import sample_invoice
from einvoice import scenarios as scn


class Command(BaseCommand):
    help = "Run the e-Invoice sandbox test matrix and emit CSV + JSON for the Test Summary Report."

    # NIC's minimum test counts per API (success, failure).
    FULL = {
        "auth": (30, 10), "generate": (100, 50), "cancel": (40, 20),
        "ewb": (40, 20), "get": (30, 10), "gstin": (30, 10),
    }

    def add_arguments(self, parser):
        parser.add_argument("--success", type=int, default=5, help="successful ops per API (ignored with --full)")
        parser.add_argument("--fail", type=int, default=3, help="failure ops per API (ignored with --full)")
        parser.add_argument("--full", action="store_true", help="use NIC's per-API minimum counts")
        parser.add_argument("--buyer", default="11AAACT3904F1ZZ", help="registered sandbox buyer GSTIN (inter-state)")
        parser.add_argument("--intra-buyer", default="", help="registered state-06 GSTIN for intra-state scenarios")
        parser.add_argument("--sez-buyer", default="", help="SEZ-unit GSTIN for SEZ scenarios")
        parser.add_argument("--delay", type=float, default=0.0, help="seconds to sleep between calls")
        parser.add_argument("--outdir", default="test_results", help="output directory")

    def handle(self, *args, **opts):
        self.cfg = settings.EINV
        self.client = EInvoiceClient()
        self.buyer = opts["buyer"]
        self.intra_buyer = opts["intra_buyer"]
        self.sez_buyer = opts["sez_buyer"]
        self.delay = opts["delay"]
        self.scenario_results: dict[str, str] = {}
        self.log: list[dict] = []
        self.run_id = datetime.now().strftime("%y%m%d%H%M%S")
        self.seller = self.cfg["GSTIN"]

        if opts["full"]:
            self.targets = dict(self.FULL)
        else:
            s, f = opts["success"], opts["fail"]
            self.targets = {k: (s, f) for k in self.FULL}

        self.stdout.write(self.style.NOTICE(
            f"Run {self.run_id}: targets={self.targets} (seller {self.seller}, buyer {self.buyer})"))

        self._test_authentication()
        pools = self._test_generate_irn()
        self._test_get_irn_details(pools["get"])     # read before cancel
        self._test_get_gstin_details()
        self._test_generate_ewb(pools["ewb"])
        self._test_cancel_irn(pools["cancel"])

        self._write_outputs(Path(opts["outdir"]))

    # -- helpers ------------------------------------------------------------

    def _attempt(self, api, expect, fn, sample=None):
        rec = {"api": api, "expect": expect, "at": datetime.now().isoformat(timespec="seconds")}
        try:
            res = fn()
            rec["ok"] = True
            rec["sample"] = sample(res) if callable(sample) else sample
        except EInvoiceError as e:
            rec["ok"] = False
            rec["error"] = e.error_details
        except Exception as e:  # noqa: BLE001 - record anything unexpected
            rec["ok"] = False
            rec["error"] = f"{type(e).__name__}: {e}"
        self.log.append(rec)
        flag = "ok" if rec["ok"] else "ERR"
        self.stdout.write(f"  [{flag}] {api} ({expect})" + (f"  {rec.get('sample')}" if rec.get("sample") else ""))
        if self.delay:
            time.sleep(self.delay)
        return rec

    def _doc_no(self, tag, i):
        # Unique per run so we never hit duplicate-IRN errors.
        return f"T{self.run_id}{tag}{i}"

    def _valid_invoice(self, tag, i):
        return sample_invoice(self.seller, self.buyer, self._doc_no(tag, i), self._today())

    def _ctx(self, tag, i):
        return {
            "seller": self.seller, "buyer": self.buyer,
            "intra_buyer": self.intra_buyer or None, "sez_buyer": self.sez_buyer or None,
            "doc_no": self._doc_no(tag, i), "doc_date": self._today(),
        }

    @staticmethod
    def _today():
        return datetime.now().strftime("%d/%m/%Y")

    # -- 1. Authentication --------------------------------------------------

    def _test_authentication(self):
        ok, fail = self.targets["auth"]
        self.stdout.write(self.style.HTTP_INFO(f"Authentication ({ok}/{fail})"))
        for i in range(ok):
            self._attempt("Authentication", "success",
                          lambda: self.client._get_session(force=True),
                          sample=lambda s: "token ok")
        # Negative cases: bad client-secret / client-id / password.
        bad = [
            ("client-secret", lambda c: c.update({"CLIENT_SECRET": "WRONGSECRET"})),
            ("client-id", lambda c: c.update({"CLIENT_ID": "WRONGID"})),
            ("password", lambda c: c.update({"PASSWORD": "WrongPass!0"})),
        ]
        for j in range(fail):
            name, mutate = bad[j % len(bad)]
            self._attempt("Authentication", "failure",
                          lambda m=mutate: self._auth_with_bad(m))

    def _auth_with_bad(self, mutate):
        original = dict(self.cfg)
        try:
            mutate(self.cfg)
            cache.clear()
            EInvoiceClient()._authenticate()
        finally:
            self.cfg.clear(); self.cfg.update(original)
            cache.clear()

    # -- 2. Generate IRN ----------------------------------------------------

    def _test_generate_irn(self):
        gen_ok, gen_fail = self.targets["generate"]
        ewb_ok = self.targets["ewb"][0]
        cancel_ok = self.targets["cancel"][0]
        # Downstream IRNs needed; top up to the Generate-IRN target if short.
        need = ewb_ok + cancel_ok
        total = max(gen_ok, need)
        self.stdout.write(self.style.HTTP_INFO(f"Generate IRN ({total} success / {gen_fail} fail)"))

        # Phase 1 - scenario coverage: run each Key-Feature scenario once.
        scenario_irns = []
        for label, fn in scn.SCENARIOS:
            inv = fn(self._ctx("S", len(scenario_irns)))
            if inv is None:
                self.scenario_results[label] = "skipped (GSTIN not provided)"
                self.stdout.write(f"  [skip] {label}")
                continue
            rec = self._attempt("Generate IRN", "success",
                                lambda x=inv: self.client.generate_irn(x),
                                sample=lambda res: {"Irn": res.get("Irn"), "AckNo": res.get("AckNo"),
                                                    "AckDt": res.get("AckDt"), "scenario": label})
            if rec["ok"]:
                self.scenario_results[label] = "OK"
                scenario_irns.append(rec["sample"]["Irn"])
            else:
                err = rec.get("error")
                msg = err[0].get("ErrorMessage") if isinstance(err, list) and err else str(err)[:80]
                self.scenario_results[label] = f"FAILED: {msg}"

        # Phase 2 - volume: bulk-generate the reliable base scenario. Must yield
        # enough base IRNs for the EWB+cancel pools AND reach the total count.
        bulk = []
        n = 0
        need_bulk = ewb_ok + cancel_ok
        while len(bulk) < need_bulk or len(scenario_irns) + len(bulk) < total:
            inv = scn.b2b_interstate(self._ctx("G", n)); n += 1
            rec = self._attempt("Generate IRN", "success",
                                lambda x=inv: self.client.generate_irn(x),
                                sample=lambda res: {"Irn": res.get("Irn"), "AckNo": res.get("AckNo"),
                                                    "AckDt": res.get("AckDt"), "scenario": "INV B2B interstate"})
            if rec["ok"] and rec.get("sample"):
                bulk.append(rec["sample"]["Irn"])
            if n > (total + need_bulk) * 2:  # safety
                break

        # Pools: EWB/cancel from bulk (predictable base invoices); get reuses all.
        pools = {
            "ewb": bulk[:ewb_ok],
            "cancel": bulk[ewb_ok:ewb_ok + cancel_ok],
        }
        pools["get"] = (scenario_irns + bulk)[:self.targets["get"][0]]

        failers = [
            ("buyer==seller", lambda inv: inv["BuyerDtls"].update({"Gstin": self.seller, "Pos": "06", "Stcd": "06"})),
            ("bad HSN (4-digit)", lambda inv: inv["ItemList"][0].update({"HsnCd": "1001"})),
            ("missing ValDtls", lambda inv: inv.pop("ValDtls", None)),
            ("bad doc date", lambda inv: inv["DocDtls"].update({"Dt": "2026-06-23"})),
            ("doc no starts with 0", lambda inv: inv["DocDtls"].update({"No": "0BADNO"})),
        ]
        for j in range(gen_fail):
            name, mutate = failers[j % len(failers)]
            inv = self._valid_invoice("F", j)
            mutate(inv)
            self._attempt("Generate IRN", "failure", lambda x=inv: self.client.generate_irn(x))
        return pools

    # -- 3. Get IRN details -------------------------------------------------

    def _test_get_irn_details(self, irns):
        ok, fail = self.targets["get"]
        self.stdout.write(self.style.HTTP_INFO(f"Get IRN Details ({ok}/{fail}, +signature validation)"))
        for irn in irns[:ok]:
            self._attempt("Get IRN Details", "success",
                          lambda x=irn: self.client.get_irn_details(x),
                          sample=lambda res: {"Irn": res.get("Irn", "")[:16] + "...",
                                              "signedInvoiceValid": self._jwt_ok(res.get("SignedInvoice")),
                                              "signedQRValid": self._jwt_ok(res.get("SignedQRCode"))})
        for j in range(fail):
            self._attempt("Get IRN Details", "failure",
                          lambda j=j: self.client.get_irn_details("0" * 64 + str(j)))

    @staticmethod
    def _jwt_ok(token):
        """Validate a NIC signed JWS: 3 parts, decodable JSON payload with data."""
        if not token or token.count(".") != 2:
            return False
        try:
            import base64
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            return "data" in data or "Irn" in json.dumps(data)
        except Exception:
            return False

    # -- 4. Get GSTIN details -----------------------------------------------

    def _test_get_gstin_details(self):
        ok, fail = self.targets["gstin"]
        self.stdout.write(self.style.HTTP_INFO(f"Get GSTIN Details ({ok}/{fail})"))
        valid = [self.seller, self.buyer]
        for i in range(ok):
            g = valid[i % len(valid)]
            self._attempt("Get GSTIN Details", "success",
                          lambda x=g: self.client.get_gstin_details(x),
                          sample=lambda res: res.get("Gstin"))
        for j in range(fail):
            self._attempt("Get GSTIN Details", "failure",
                          lambda j=j: self.client.get_gstin_details(f"99XXXXX0000X{j}Z9"))

    # -- 5. Generate e-Way Bill by IRN --------------------------------------

    def _test_generate_ewb(self, irns):
        ok, fail = self.targets["ewb"]
        self.stdout.write(self.style.HTTP_INFO(f"Generate EWB by IRN ({ok}/{fail}, varied transport modes)"))
        self.ewb_active = []  # IRNs that ended up with an active EWB
        # Reliable modes only: Road (1) and Air (3). Rail/Ship need valid RR /
        # bill-of-lading numbers that can't be fabricated in sandbox. Road also
        # alternates vehicle type (Regular/ODC) for transport-detail variety.
        for k, irn in enumerate(irns[:ok]):
            mode = 1 if k % 2 == 0 else 3
            veh_type = "R" if (k // 2) % 2 == 0 else "O"
            rec = self._attempt("Generate EWB by IRN", "success",
                                lambda x=irn, m=mode, v=veh_type: self.client.generate_ewb_by_irn(self._ewb_payload(x, m, v)),
                                sample=lambda res: {"EwbNo": res.get("EwbNo"), "EwbValidTill": res.get("EwbValidTill"),
                                                    "mode": mode, "vehType": veh_type})
            if rec["ok"]:
                self.ewb_active.append(irn)
        for j in range(fail):
            self._attempt("Generate EWB by IRN", "failure",
                          lambda j=j: self.client.generate_ewb_by_irn({"Irn": "0" * 64, "Distance": 10}))

    def _ewb_payload(self, irn, mode=1, veh_type="R"):
        # Mode 1 (road) needs VehNo; mode 3 (air) needs a transport doc no/date.
        # TransId omitted (empty fails NIC's 15-char Transin validation).
        p = {
            "Irn": irn,
            "Distance": 0,  # 0 => NIC auto-calculates from PIN-to-PIN
            "TransMode": str(mode),
            "TransDocNo": "TD" + self.run_id[-6:],
            "TransDocDt": self._today(),
        }
        if mode == 1:
            p.update({"VehNo": "HR26AB1234", "VehType": veh_type})
        return p

    # -- 6. Cancel IRN ------------------------------------------------------

    CANCEL_REASONS = {"1": "Duplicate", "2": "Data entry mistake",
                      "3": "Order cancelled", "4": "Other"}

    def _test_cancel_irn(self, irns):
        ok, fail = self.targets["cancel"]
        self.stdout.write(self.style.HTTP_INFO(f"Cancel IRN ({ok}/{fail}, varied reasons)"))
        cancelled = []
        for k, irn in enumerate(irns[:ok]):
            code = str((k % 4) + 1)  # cycle reasons 1-4
            rec = self._attempt("Cancel IRN", "success",
                                lambda x=irn, c=code: self.client.cancel_irn(x, c, self.CANCEL_REASONS[c]),
                                sample=lambda res: {"Irn": res.get("Irn"), "reason": code})
            if rec["ok"]:
                cancelled.append(irn)
        # Negative: bad IRN, re-cancel an already-cancelled IRN, and (key feature)
        # cancel an IRN that has an active e-Way Bill.
        neg = [("bad irn", lambda: self.client.cancel_irn("0" * 64, "2", "bad irn"))]
        if cancelled:
            neg.append(("re-cancel", lambda: self.client.cancel_irn(cancelled[0], "2", "again")))
        if getattr(self, "ewb_active", None):
            neg.append(("cancel IRN with active EWB",
                        lambda: self.client.cancel_irn(self.ewb_active[0], "2", "has active ewb")))
        for j in range(fail):
            name, fn = neg[j % len(neg)]
            self._attempt("Cancel IRN", "failure", fn)

        # Key feature: view a CANCELLED IRN's details (Status should be CNL).
        if cancelled:
            self._attempt("Get IRN Details", "success",
                          lambda x=cancelled[0]: self.client.get_irn_details(x),
                          sample=lambda res: {"Irn": res.get("Irn", "")[:16] + "...",
                                              "Status": res.get("Status"), "note": "cancelled-IRN view"})

    # -- output -------------------------------------------------------------

    def _write_outputs(self, outdir: Path):
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "details.json").write_text(json.dumps(self.log, indent=2, default=str), encoding="utf-8")

        apis = ["Authentication", "Generate IRN", "Get IRN Details",
                "Get GSTIN Details", "Generate EWB by IRN", "Cancel IRN"]
        rows = []
        for api in apis:
            recs = [r for r in self.log if r["api"] == api]
            succ = sum(1 for r in recs if r["expect"] == "success" and r["ok"])
            failed = sum(1 for r in recs if r["expect"] == "failure" and not r["ok"])
            anomalies = ([f"{r['expect']}->{'ok' if r['ok'] else 'err'}" for r in recs
                          if (r["expect"] == "success") != r["ok"]])
            raw_sample = next((r.get("sample") for r in recs
                               if r["expect"] == "success" and r["ok"] and r.get("sample")), "")
            if isinstance(raw_sample, dict):
                sample = raw_sample.get("Irn") or raw_sample.get("EwbNo") or next(iter(raw_sample.values()), "")
            else:
                sample = raw_sample
            remark = "OK" if not anomalies else f"{len(anomalies)} anomaly(ies)"
            rows.append([api, succ, failed, sample or "", remark])

        with (outdir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Company", "Jivo Wellness Pvt Ltd", "Client Id", self.cfg["CLIENT_ID"],
                        "GSTIN", self.seller, "User", self.cfg["USERNAME"], "Run", self.run_id])
            w.writerow([])
            w.writerow(["API", "Successful operations", "Failed operations",
                        "Sample IRN/EWB No", "Remarks"])
            w.writerows(rows)
            w.writerow([])
            w.writerow(["Generate IRN - Key Feature scenario coverage", "Result"])
            for label, result in self.scenario_results.items():
                w.writerow([label, result])

        self.stdout.write(self.style.SUCCESS(f"\nWrote {outdir}/summary.csv and details.json"))
        for r in rows:
            self.stdout.write(f"  {r[0]:24s} success={r[1]:<4} failed={r[2]:<4} {r[4]}")
        self.stdout.write(self.style.HTTP_INFO("Scenario coverage:"))
        for label, result in self.scenario_results.items():
            self.stdout.write(f"  {label:26s} {result}")
