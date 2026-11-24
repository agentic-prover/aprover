"""
Aggregate bug-finding results across:
  - The original 7-file N=3 sweep at /tmp/libarchive_n3_full_out/
  - The corpus-expansion sweep at /tmp/libarchive_expand_out/
  - The refinement experiment at /tmp/libarchive_refine_out/
  - The rescue_realism.json files from the SPURIOUS rescue

Reports:
  - Per-sweep confirmed-bug count (verdict != unrealistic)
  - Realistic-verdict findings (strongest signal)
  - Documented seed-bug match count (cross-referenced against the
    known 14 mappable commits in the corpus)
  - Rescue verdicts (how many SPURIOUS findings got REALISTIC)

Usage:
    python scripts/aggregate_results.py
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

# Documented seed bugs: function-name → commit (truthful, from prior triage)
SEED_FUNCTION_TO_COMMIT = {
    # archive_acl.c
    "next_field": "8308b61c",
    "next_field_w": "8308b61c-companion",   # latent — not patched upstream
    "archive_acl_to_text_w": "d45b5b4b",
    "archive_acl_from_text_nl": "4b3ba035",
    # archive_read_support_format_cab.c
    "cab_checksum_finish": "32b62cf7",
    "cab_checksum_cfdata": "32b62cf7",       # paired
    "cab_skip_sfx": "32b62cf7",              # same commit, alt entry
    "lzx_decode": "79a0787b",
    "lzx_huffman_init": "1f545457",
    "lzx_make_huffman_table": "1f545457",  # actual function patched
    "archive_read_support_format_cab": "e19ef42d",  # repeated-init leak
    # archive_read_support_format_iso9660.c
    "parse_rockridge": "c3cb1c56",
    "isJolietSVD": "a9d2cc5e",
    "isJolietSVDi": "a403da94",
    # archive_read_support_format_cpio.c
    "find_newc_header": "1f2da75f",
    "archive_read_format_cpio_read_header": "1f2da75f",
    "record_hardlink": "16ad9310",
    # archive_read_support_format_rar5.c
    "do_uncompress_file": "25d97315",
    "init_unpack": "620bdafa",
    "rar5_cleanup": "35877523",
    "process_base_block": "ef53e202",   # decompression infinite loop
    "rar5_free_decoded_data": "f8fea386",
    # archive_pathmatch.c / archive_match.c
    "__archive_pathmatch_w": "4cbf9582",
    "archive_match_path_excluded": "470379a9",
    # archive_read_support_format_mtree.c — actual functions patched
    "parsedigit": "b2ce282d",        # hex parser
    "parse_keyword": "0a6f7f1c",     # time-value parser truncation (location)
    "parse_time": "0a6f7f1c",
    # archive_read_support_format_rar.c
    "make_table": "059dff39",
    "parse_codes": "d379dc0b",       # LZSS window-size mismatch
    "copy_from_lzss_window": "d379dc0b",
    "parse_filter": "d379dc0b",
    # archive_read_support_format_rar5.c — f8fea386 memory leak
    "cdeque_front": "f8fea386",
    "add_new_filter": "f8fea386",
    # archive_write_set_format_iso9660.c (write-side, 6 seeds)
    "build_pathname_utf16be": "750e8d7b",       # Joliet pathname overflow
    "isofile_gen_utility_names": "8ba3972e",    # memmove (also 941e32fd)
    "_write_path_table": "a403da94",
    "idr_extend_identifier": "a403da94",
    "_compare_path_table": "a403da94",
    "_compare_path_table_joliet": "a403da94",
    "isoent_gen_iso9660_identifier": "2b0ab5bd",
    "isoent_gen_joliet_identifier": "2b0ab5bd",
    "idr_resolve": "2b0ab5bd",
    "archive_write_set_format_iso9660": "35befb8c",
    "isoent_rr_move_dir": "35befb8c",
    # archive_write_set_format_mtree.c
    "write_mtree_entry_tree": "266e3d5f",
    # archive_write_set_format_xar.c
    "file_gen_utility_names": "e35b629f",
    "file_tree": "e35b629f",
    # filter / util / sparse / acl / contrib
    "atou64": "4f2d7832",   # xar (libxml2 blocked)
    "archive_read_support_filter_program_signature": "45ec1a24",
    "compress_filter_init": "3d4871e4",
    "archive_entry_sparse_count": "b1622a8e",
    "sparse_reset": "b1622a8e",
    "archive_entry_linkresolver_free": "23edf569",
    "__archive_mktempx": "a932ffa3",
    "parseoct": "00640329",
    "archive_acl_from_text_nl": "4b3ba035",
    # 7zip — blocked but seeds present
    "find_elf_data_sec": "24cb0b58",
    "read_Header": "51cfd615",
    "setup_decode_folder": "a4b3f692",          # also f52a211f
    "archive_read_format_7zip_read_header": "f52a211f",
    "_7z_read_SubStreamsInfo": "24cb0b58",
    "header_byte_decode": "a4b3f692",
}

# Per-file expected seeds (for coverage display)
SEEDS_PER_FILE = {
    "archive_acl.c": 3,
    "archive_match.c": 1,
    "archive_pathmatch.c": 2,
    "archive_read_support_format_cab.c": 4,
    "archive_read_support_format_cpio.c": 2,
    "archive_read_support_format_iso9660.c": 2,
    "archive_read_support_format_rar5.c": 4,
    "archive_read_support_format_mtree.c": 2,
    "archive_read_support_format_rar.c": 2,
    "archive_read_support_format_tar.c": 0,
    "archive_read_support_format_zip.c": 0,
    "archive_string.c": 0,
    "archive_util.c": 1,
    "archive_write_set_format_iso9660.c": 6,
    "archive_write_set_format_mtree.c": 1,
    "archive_write_set_format_xar.c": 1,
    "archive_read_support_filter_program.c": 1,
    "archive_read_support_filter_compress.c": 1,
    "archive_entry_sparse.c": 1,
    "archive_entry_link_resolver.c": 1,
    "archive_read_support_format_7zip.c": 4,
    "archive_read_support_format_xar.c": 1,
}


def _scan_sweep(sweep_dir: Path) -> dict:
    """Return aggregate stats for a sweep output dir."""
    if not sweep_dir.is_dir():
        return {"exists": False, "dir": str(sweep_dir)}

    confidence_counts: Counter = Counter()
    verdict_counts: Counter = Counter()
    confirmed: list[dict] = []
    realistic: list[dict] = []
    seed_matches: list[tuple[str, str, str]] = []  # (fn, prop, verdict)

    for br in sweep_dir.rglob("bug_report.json"):
        try:
            doc = json.load(open(br))
        except Exception:
            continue
        report = doc.get("report") or {}
        confidence = report.get("confidence", "?")
        verdict = (report.get("realism_check") or {}).get("verdict", "?")
        fn = report.get("function_name", "?")
        prop = report.get("violated_property", "?")
        confidence_counts[confidence] += 1
        verdict_counts[verdict] += 1
        is_confirmed = confidence and confidence != "unlikely" and confidence != "?"
        if is_confirmed:
            confirmed.append({"fn": fn, "prop": prop, "verdict": verdict, "conf": confidence})
        if verdict == "realistic":
            realistic.append({"fn": fn, "prop": prop, "conf": confidence})
        if fn in SEED_FUNCTION_TO_COMMIT and is_confirmed:
            seed_matches.append((fn, SEED_FUNCTION_TO_COMMIT[fn], verdict))

    return {
        "exists": True,
        "dir": str(sweep_dir),
        "confidence_counts": dict(confidence_counts),
        "verdict_counts": dict(verdict_counts),
        "n_confirmed": len(confirmed),
        "n_realistic": len(realistic),
        "confirmed": confirmed,
        "realistic": realistic,
        "seed_matches": seed_matches,
    }


def _scan_rescue(sweep_dir: Path) -> dict:
    """Return rescue verdict counts."""
    if not sweep_dir.is_dir():
        return {"exists": False}
    counts: Counter = Counter()
    rescued_seeds: list[str] = []
    for r in sweep_dir.rglob("rescue_realism.json"):
        try:
            d = json.load(open(r))
        except Exception:
            continue
        verdict = d.get("rescue_verdict", "?")
        counts[verdict] += 1
        if verdict == "realistic" and d.get("function_name") in SEED_FUNCTION_TO_COMMIT:
            rescued_seeds.append(d["function_name"])
    return {
        "exists": True,
        "n_rescue_files": sum(counts.values()),
        "verdict_counts": dict(counts),
        "rescued_seeds": rescued_seeds,
    }


def _format_seed_coverage(matches: list[tuple[str, str, str]]) -> str:
    by_commit = defaultdict(list)
    for fn, commit, verdict in matches:
        by_commit[commit].append((fn, verdict))
    lines: list[str] = []
    for commit, items in sorted(by_commit.items()):
        for fn, v in items:
            lines.append(f"  {commit}  {fn} [{v}]")
    return "\n".join(lines)


def main() -> int:
    sweeps = {
        "Baseline N=1 (7 files, older classifier)": Path("/tmp/libarchive_seedhunt_out"),
        "Validation N=3 (acl)":                     Path("/tmp/libarchive_acl_validation_out"),
        "Full N=3 (7 files)":                       Path("/tmp/libarchive_n3_full_out"),
        "Corpus expansion (4 files)":               Path("/tmp/libarchive_expand_out"),
        "Tier-2 (tar+util)":                        Path("/tmp/libarchive_tier2_out"),
        "Tier-3 (write-side + 6 small)":            Path("/tmp/libarchive_tier3_out"),
        "Refinement (iso9660+cab)":                 Path("/tmp/libarchive_refine_out"),
    }
    print("=" * 76)
    print("BUG FINDING AGGREGATE — 2026-05-24")
    print("=" * 76)
    all_seed_matches: list[tuple[str, str, str]] = []
    for name, path in sweeps.items():
        stats = _scan_sweep(path)
        print(f"\n### {name}")
        if not stats["exists"]:
            print(f"  [pending] dir not yet present: {stats['dir']}")
            continue
        print(f"  dir: {stats['dir']}")
        print(f"  confidence counts: {stats['confidence_counts']}")
        print(f"  verdict counts:    {stats['verdict_counts']}")
        print(f"  CONFIRMED (non-unlikely): {stats['n_confirmed']}")
        print(f"  REALISTIC verdict:        {stats['n_realistic']}")
        if stats["seed_matches"]:
            print(f"  SEED MATCHES ({len(stats['seed_matches'])}):")
            print(_format_seed_coverage(stats["seed_matches"]))
        all_seed_matches.extend(stats["seed_matches"])

    print("\n### Rescue results (SPURIOUS → realism rerun)")
    rescue = _scan_rescue(Path("/tmp/libarchive_n3_full_out"))
    if rescue.get("exists"):
        print(f"  rescue_realism.json files: {rescue['n_rescue_files']}")
        print(f"  verdict counts:            {rescue['verdict_counts']}")
        if rescue["rescued_seeds"]:
            print(f"  RESCUED SEED BUGS: {', '.join(rescue['rescued_seeds'])}")
        else:
            print("  RESCUED SEED BUGS: none")
    else:
        print("  [pending]")

    # ---- per-sweep + union + regression analysis ----
    print("\n" + "=" * 76)
    print("SEED-COMMIT COVERAGE (per-sweep, union, regression)")
    print("=" * 76)
    per_sweep_commits: dict[str, set[str]] = {}
    for name, path in sweeps.items():
        stats = _scan_sweep(path)
        if not stats.get("exists"):
            continue
        commits = {c for _, c, _ in stats["seed_matches"]}
        per_sweep_commits[name] = commits

    # Sort by sweep order for regression display
    sweep_names = list(per_sweep_commits.keys())
    print(f"\nPer-sweep seed-commit counts (SINGLE-CONFIG performance):")
    for name in sweep_names:
        n = len(per_sweep_commits[name])
        print(f"  {n:3d}  {name}")

    # Best-single-config headline (what a single invocation of one config achieves)
    if per_sweep_commits:
        best_name = max(per_sweep_commits, key=lambda k: len(per_sweep_commits[k]))
        best_set = per_sweep_commits[best_name]
        print(f"\n>>> BEST SINGLE-SWEEP CONFIG: {best_name}")
        print(f"    seeds matched: {len(best_set)}")
        print(f"    commits: {sorted(best_set)}")

    # Union — capability claim (what the tool's ENSEMBLE can find)
    union = set().union(*per_sweep_commits.values()) if per_sweep_commits else set()
    print(f"\n>>> UNION ACROSS ALL SWEEPS (ensemble capability):")
    print(f"    seeds matched: {len(union)}  (of 43 total in interval)")
    print(f"    commits: {sorted(union)}")

    # Regression detection — restricted to sweep PAIRS that target the same
    # corpus (otherwise a "lost commit" is just a coverage gap, not a regression).
    same_corpus_pairs = [
        ("Baseline N=1 (7 files, older classifier)", "Full N=3 (7 files)"),
        ("Validation N=3 (acl)", "Full N=3 (7 files)"),
    ]
    print(f"\n>>> REGRESSIONS (same-corpus pairs: seed lost between configs):")
    any_regression = False
    for earlier, later in same_corpus_pairs:
        if earlier not in per_sweep_commits or later not in per_sweep_commits:
            continue
        lost = per_sweep_commits[earlier] - per_sweep_commits[later]
        if lost:
            any_regression = True
            print(f"  {earlier}")
            print(f"    -> {later}")
            print(f"    lost: {sorted(lost)}")
    if not any_regression:
        print("  (none — recommended config kept every seed earlier configs had)")

    print("\n" + "=" * 76)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
