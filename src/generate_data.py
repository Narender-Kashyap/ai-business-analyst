"""Generate synthetic payments data with a planted story.

Run from the project root:  python src/generate_data.py
"""
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

# ---------- Configuration ----------
SEED = 42                      # same seed -> same data every run
START_DATE = "2026-09-21"      # a Monday
NUM_DAYS = 56                  # 8 weeks
TXNS_PER_DAY = 1500
DB_PATH = Path("data/payments.db")

BANKS = ["Bank A", "Bank B", "Bank C", "Bank D", "Bank E", "Bank F"]

# Normal success probability of each payment method
METHOD_SUCCESS = {
    "UPI": 0.95,
    "Wallet": 0.93,
    "Credit Card": 0.92,
    "Debit Card": 0.90,
    "Net Banking": 0.82,
}

# Share of traffic per method: weeks 1-7 vs week 8 (the mix shift)
NORMAL_MIX = {"UPI": 0.50, "Wallet": 0.10, "Credit Card": 0.15,
              "Debit Card": 0.15, "Net Banking": 0.10}
WEEK8_MIX = {"UPI": 0.433, "Wallet": 0.087, "Credit Card": 0.13,
             "Debit Card": 0.13, "Net Banking": 0.22}

# The bank outage (the rate drop)
OUTAGE_BANK = "Bank C"
OUTAGE_DATES = ["2026-11-11", "2026-11-12"]
OUTAGE_FACTOR = 0.60           # success prob is multiplied by this

CATEGORIES = ["grocery", "electronics", "travel", "food", "utilities"]
REGIONS = ["north", "south", "east", "west"]
NUM_MERCHANTS = 200
FAILURE_REASONS = ["BANK_DECLINED", "TIMEOUT", "INSUFFICIENT_FUNDS", "USER_ABANDONED"]


def build_dimensions(rng):
    """Create the three small dimension tables."""
    banks = pd.DataFrame({
        "bank_id": range(1, len(BANKS) + 1),
        "bank_name": BANKS,
    })
    payment_methods = pd.DataFrame({
        "method_id": range(1, len(METHOD_SUCCESS) + 1),
        "method_name": list(METHOD_SUCCESS.keys()),
    })
    merchants = pd.DataFrame({
        "merchant_id": range(1, NUM_MERCHANTS + 1),
        "merchant_name": [f"Merchant {i:03d}" for i in range(1, NUM_MERCHANTS + 1)],
        "category": rng.choice(CATEGORIES, size=NUM_MERCHANTS),
        "region": rng.choice(REGIONS, size=NUM_MERCHANTS),
    })
    return banks, payment_methods, merchants

def generate_transactions(rng):
    """Create clean transactions, day by day, with the planted story."""
    method_names = list(METHOD_SUCCESS.keys())
    days = pd.date_range(START_DATE, periods=NUM_DAYS, freq="D")
    all_days = []

    for day_index, day in enumerate(days):
        n = TXNS_PER_DAY
        week = day_index // 7 + 1                     # 1..8
        mix = WEEK8_MIX if week == 8 else NORMAL_MIX

        # 1. Who, where, how
        methods = rng.choice(method_names, size=n, p=[mix[m] for m in method_names])
        bank_ids = rng.integers(1, len(BANKS) + 1, size=n)
        merchant_ids = rng.integers(1, NUM_MERCHANTS + 1, size=n)
        seconds = rng.integers(0, 24 * 3600, size=n)
        timestamps = day + pd.to_timedelta(seconds, unit="s")
        amounts = np.round(rng.lognormal(mean=6.5, sigma=1.0, size=n), 2)

        # 2. Success probability: method rate, cut during the outage
        p_success = np.array([METHOD_SUCCESS[m] for m in methods])
        outage_bank_id = BANKS.index(OUTAGE_BANK) + 1
        in_outage = (day.strftime("%Y-%m-%d") in OUTAGE_DATES) & (bank_ids == outage_bank_id)
        p_success = np.where(in_outage, p_success * OUTAGE_FACTOR, p_success)

        # 3. Outcome: 0.5% PENDING, else SUCCESS/FAILED by probability
        u = rng.random(n)
        status = np.where(u < p_success, "SUCCESS", "FAILED")
        status = np.where(rng.random(n) < 0.005, "PENDING", status)

        # 4. Failure reason only for FAILED rows
        reasons = rng.choice(FAILURE_REASONS, size=n)
        reasons = np.where(in_outage, "ISSUER_UNAVAILABLE", reasons)
        reasons = np.where(status == "FAILED", reasons, None)

        method_ids = [method_names.index(m) + 1 for m in methods]
        all_days.append(pd.DataFrame({
            "txn_ts": timestamps,
            "merchant_id": merchant_ids,
            "bank_id": bank_ids,
            "method_id": method_ids,
            "amount": amounts,
            "status": status,
            "failure_reason": reasons,
        }))

    txns = pd.concat(all_days, ignore_index=True)
    txns = txns.sort_values("txn_ts").reset_index(drop=True)
    txns.insert(0, "txn_id", [f"T{i:06d}" for i in range(1, len(txns) + 1)])
    return txns

def build_settlements(clean_txns):
    """Second source: daily settled count/amount per merchant.

    Built from CLEAN successful txns. Settlement cutoff is 23:30,
    so late-night txns settle the next day -> daily counts disagree.
    """
    ok = clean_txns[clean_txns["status"] == "SUCCESS"].copy()
    ok["settlement_date"] = (ok["txn_ts"] + pd.Timedelta(minutes=30)).dt.strftime("%Y-%m-%d")
    settlements = (ok.groupby(["settlement_date", "merchant_id"])
                     .agg(txn_count=("txn_id", "count"), gross_amount=("amount", "sum"))
                     .reset_index())
    settlements["gross_amount"] = settlements["gross_amount"].round(2)
    return settlements


def add_dirt(rng, txns):
    """Plant data-quality problems: duplicates and missing values."""
    dirty = txns.copy()
    dirty["bank_id"] = dirty["bank_id"].astype("Int64")   # integer column that allows NULL

    # Missing bank_id on ~1% of rows
    null_mask = rng.random(len(dirty)) < 0.01
    dirty.loc[null_mask, "bank_id"] = pd.NA

    # Missing failure_reason on ~10% of FAILED rows
    failed = dirty["status"] == "FAILED"
    dirty.loc[failed & (rng.random(len(dirty)) < 0.10), "failure_reason"] = None

    # Duplicate ~0.5% of rows (gateway retry logging), same txn_id
    dupes = dirty.sample(frac=0.005, random_state=SEED)
    dirty = pd.concat([dirty, dupes], ignore_index=True)
    dirty = dirty.sort_values("txn_ts").reset_index(drop=True)
    return dirty


def save_to_sqlite(tables):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        for name, df in tables.items():
            out = df.copy()
            if "txn_ts" in out.columns:
                out["txn_ts"] = out["txn_ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
            out.to_sql(name, conn, if_exists="replace", index=False)
        # Clean view: duplicates removed. The AI only ever queries this view.
        conn.execute("DROP VIEW IF EXISTS transactions_clean")
        conn.execute("CREATE VIEW transactions_clean AS SELECT DISTINCT * FROM transactions")


def main():
    rng = np.random.default_rng(SEED)
    banks, payment_methods, merchants = build_dimensions(rng)
    clean = generate_transactions(rng)
    settlements = build_settlements(clean)
    transactions = add_dirt(rng, clean)

    save_to_sqlite({
        "banks": banks,
        "payment_methods": payment_methods,
        "merchants": merchants,
        "transactions": transactions,
        "settlements": settlements,
    })

    print(f"Saved to {DB_PATH}")
    print(f"banks={len(banks)} methods={len(payment_methods)} merchants={len(merchants)}")
    print(f"transactions={len(transactions)} (clean {len(clean)}, duplicates {len(transactions) - len(clean)})")
    print(f"settlements={len(settlements)}")

if __name__ == "__main__":
    main()