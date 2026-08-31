"""Regression tests for the dashboard's coarse asset-class mapping.

ADDED 2026-08-31 after the "Exposure by asset class" panel was found showing raw enum names
as buckets. app.py's _COARSE_CLASS covered only 14 of the 48 asset_class values that
instruments.py actually defines, and _asset_class_for() falls back to `.title()` for
anything unmapped -- so two thirds of the universe rendered as its own one-instrument
bucket with a name like "Muni_Hy" / "Us_Sector" / "Commodity2". Confirmed live on paper:
HYD sat in a "Muni_Hy" bucket at 76% of exposure.

app.py can't be imported in a test (`ui.run()` at module level), so the mapping is read out
of the source rather than imported -- unusual, but it makes the completeness property
enforceable, which is the whole point.

Run:  uv run python -m dashboard.tests.test_asset_classes
"""
from __future__ import annotations

import ast
import pathlib

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def _coarse_class_map() -> dict:
    """Parse _COARSE_CLASS out of app.py without importing it (ui.run() would block)."""
    src = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "_COARSE_CLASS":
                    return ast.literal_eval(node.value)
    raise AssertionError("_COARSE_CLASS not found in app.py")


def _universe_asset_classes() -> dict:
    """{asset_class: [instrument keys]} across every *_BY_KEY table in instruments.py."""
    from dashboard import instruments as I
    seen: dict[str, list] = {}
    for name in dir(I):
        if not name.endswith("_BY_KEY"):
            continue
        for key, inst in (getattr(I, name) or {}).items():
            ac = getattr(inst, "asset_class", None)
            if ac:
                seen.setdefault(ac, []).append(key)
    return seen


def test_every_asset_class_has_a_coarse_bucket():
    print("\n_COARSE_CLASS: every asset_class in the universe must map to a coarse bucket, "
          "or the panel renders its raw enum name (the 2026-08-31 'Muni_Hy' bug):")
    mapping = _coarse_class_map()
    universe = _universe_asset_classes()
    missing = sorted(ac for ac in universe if ac not in mapping)
    if missing:
        print(f"    unmapped: {[(m, sorted(universe[m])[:4]) for m in missing]}")
    check("no unmapped asset_class values", missing, [])
    check("universe is non-empty (guards against a silently empty lookup)",
          len(universe) > 10, True)


def test_coarse_buckets_are_a_small_readable_set():
    print("_COARSE_CLASS: the POINT is grouping -- the bucket names must stay a short, "
          "human set, not drift back toward one bucket per instrument:")
    mapping = _coarse_class_map()
    buckets = sorted(set(mapping.values()))
    print(f"    buckets: {buckets}")
    check("at most 12 distinct buckets", len(buckets) <= 12, True)
    check("no bucket name contains an underscore (raw-enum leak)",
          [b for b in buckets if "_" in b], [])
    check("every bucket is Title Case",
          [b for b in buckets if b != b[0].upper() + b[1:]], [])


def test_known_instruments_land_in_the_expected_bucket():
    print("_COARSE_CLASS: spot-checks, including the exact instrument that exposed the bug:")
    mapping = _coarse_class_map()
    universe = _universe_asset_classes()

    def bucket_of(key):
        for ac, keys in universe.items():
            if key in keys:
                return mapping.get(ac)
        return None

    check("HYD (muni_hy) -> Credit, not 'Muni_Hy'", bucket_of("HYD"), "Credit")
    check("XLK (us_sector) -> Equity", bucket_of("XLK"), "Equity")
    check("LQD (ig_credit) -> Credit", bucket_of("LQD"), "Credit")
    check("VNQ (reit) -> REITs", bucket_of("VNQ"), "REITs")
    check("AMLP (mlp) -> Commodities", bucket_of("AMLP"), "Commodities")
    check("CPER (metal) -> Metals", bucket_of("CPER"), "Metals")
    check("TIP (inflation) -> Inflation", bucket_of("TIP"), "Inflation")
    check("SHY (rate) -> Rates", bucket_of("SHY"), "Rates")


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
            except AssertionError:
                pass
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
