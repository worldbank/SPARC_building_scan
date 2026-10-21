#!/usr/bin/env python3
"""Flag AlphaEarth spectral outliers (Isolation Forest) and produce t-SNE
diagnostic plots for one prepared city's GEE-enriched building footprints.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.manifold import TSNE
from sklearn.preprocessing import MinMaxScaler, StandardScaler

from city_workspace import add_data_root_arg, enable_logging, load_city, resolve_data_root, skip_existing


ID_FIELD = "integrated_id"
EXPECTED_ALPHA_BANDS = 64
IF_CONTAMINATION = 0.05
IF_N_ESTIMATORS = 300
TSNE_PERPLEXITY = 30
TSNE_MAX_SAMPLES = 20_000  # t-SNE is diagnostic-plot-only; doesn't scale to a whole city
RANDOM_STATE = 42
PLOT_SIZE = (8, 6)


def find_alphaearth_columns(df: pd.DataFrame) -> list[str]:
    return sorted(c for c in df.columns if str(c).lower().startswith("alphaearth_"))


def prepare_alphaearth(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, np.ndarray, list[str]]:
    alpha_columns = find_alphaearth_columns(gdf)
    if not alpha_columns:
        raise ValueError("No AlphaEarth columns found (expected 'alphaearth_' prefix).")
    if len(alpha_columns) != EXPECTED_ALPHA_BANDS:
        print(f"WARNING: expected {EXPECTED_ALPHA_BANDS} AlphaEarth bands, found {len(alpha_columns)}.")

    gdf = gdf.copy()
    gdf[alpha_columns] = gdf[alpha_columns].apply(pd.to_numeric, errors="coerce")
    missing = gdf[alpha_columns].isna().any(axis=1).sum()
    print(f"Buildings with missing AlphaEarth values: {missing:,}")

    analysis = gdf.dropna(subset=alpha_columns).reset_index(drop=True)
    print(f"Buildings retained for Isolation Forest/t-SNE: {len(analysis):,}")
    X = analysis[alpha_columns].to_numpy(dtype=np.float32)
    return analysis, X, alpha_columns


def run_isolation_forest(X: np.ndarray):
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = IsolationForest(
        contamination=IF_CONTAMINATION, n_estimators=IF_N_ESTIMATORS,
        random_state=RANDOM_STATE, n_jobs=-1,
    )
    model.fit(X_scaled)
    labels = model.predict(X_scaled)  # -1 = anomaly, 1 = normal
    outlier = labels == -1
    score = -model.decision_function(X_scaled)  # higher = more anomalous
    normalized = MinMaxScaler().fit_transform(score.reshape(-1, 1)).ravel()

    print(f"Isolation Forest outliers: {outlier.sum():,} ({outlier.mean() * 100:.2f}%)")
    return X_scaled, labels, score, normalized, outlier


def sample_for_tsne(
    df: gpd.GeoDataFrame, X_scaled: np.ndarray, max_samples: int
) -> tuple[gpd.GeoDataFrame, np.ndarray]:
    """t-SNE output is diagnostic-plot-only (never used in the screening rules),
    and sklearn's implementation doesn't scale to a whole city's worth of
    buildings -- cap it to a random sample."""
    if len(df) <= max_samples:
        return df.reset_index(drop=True), X_scaled
    rng = np.random.RandomState(RANDOM_STATE)
    index = np.sort(rng.choice(len(df), size=max_samples, replace=False))
    print(f"Sampling {max_samples:,} of {len(df):,} buildings for t-SNE (diagnostic plots only).")
    return df.iloc[index].reset_index(drop=True), X_scaled[index]


def create_tsne(df: gpd.GeoDataFrame, X_scaled: np.ndarray) -> gpd.GeoDataFrame:
    df = df.copy().reset_index(drop=True)
    n_samples = X_scaled.shape[0]
    if n_samples < 6:
        raise ValueError("At least 6 samples are required for t-SNE.")
    perplexity = min(TSNE_PERPLEXITY, max(5, (n_samples - 1) // 3))
    perplexity = min(perplexity, n_samples - 1)
    tsne = TSNE(
        n_components=2, perplexity=perplexity, learning_rate="auto",
        init="pca", random_state=RANDOM_STATE,
    )
    coords = tsne.fit_transform(np.asarray(X_scaled, dtype=np.float32))
    df["TSNE1"], df["TSNE2"] = coords[:, 0], coords[:, 1]
    return df


def plot_tsne_by_class(df: pd.DataFrame, output_dir: Path, slug: str) -> None:
    fig, ax = plt.subplots(figsize=PLOT_SIZE)
    classes = sorted(df["dw_top_class_name"].dropna().unique())
    for value in classes:
        mask = (df["dw_top_class_name"] == value).to_numpy(dtype=bool)
        ax.scatter(df.loc[mask, "TSNE1"], df.loc[mask, "TSNE2"], s=15, alpha=0.6, label=str(value))
    outlier_mask = df["isolation_forest_outlier"].to_numpy(dtype=bool)
    ax.scatter(
        df.loc[outlier_mask, "TSNE1"], df.loc[outlier_mask, "TSNE2"],
        s=60, facecolors="none", edgecolors="black", linewidths=1,
        label="Isolation Forest Outlier",
    )
    ax.set_xlabel("t-SNE 1"); ax.set_ylabel("t-SNE 2")
    ax.set_title("AlphaEarth t-SNE by Dynamic World Class")
    ax.legend(bbox_to_anchor=(1.05, 1), loc="upper left")
    plt.tight_layout()
    path = output_dir / f"tsne_by_class_{slug}.png"
    fig.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {path}")


def plot_tsne_by_anomaly(df: pd.DataFrame, output_dir: Path, slug: str) -> None:
    fig, ax = plt.subplots(figsize=PLOT_SIZE)
    outlier_mask = df["isolation_forest_outlier"].to_numpy(dtype=bool)
    ax.scatter(df.loc[~outlier_mask, "TSNE1"], df.loc[~outlier_mask, "TSNE2"], s=15, alpha=0.5, label="Normal")
    ax.scatter(
        df.loc[outlier_mask, "TSNE1"], df.loc[outlier_mask, "TSNE2"],
        s=40, alpha=0.9, label="Isolation Forest Outlier",
    )
    ax.set_xlabel("t-SNE 1"); ax.set_ylabel("t-SNE 2")
    ax.set_title("AlphaEarth t-SNE by Isolation Forest")
    ax.legend()
    plt.tight_layout()
    path = output_dir / f"tsne_by_anomaly_{slug}.png"
    fig.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {path}")


def plot_tsne_by_google25d(df: pd.DataFrame, output_dir: Path, slug: str) -> None:
    confidence = pd.to_numeric(df["google25d_max_confidence"], errors="coerce")
    bucket = pd.Series(np.select(
        [confidence.isna(), confidence < 0.1, confidence < 0.5],
        ["No data", "Low confidence (<0.1)", "Medium confidence (<0.5)"],
        default="High confidence (>=0.5)",
    ), index=df.index)
    fig, ax = plt.subplots(figsize=PLOT_SIZE)
    for value in ["No data", "Low confidence (<0.1)", "Medium confidence (<0.5)", "High confidence (>=0.5)"]:
        mask = (bucket == value).to_numpy(dtype=bool)
        if mask.any():
            ax.scatter(df.loc[mask, "TSNE1"], df.loc[mask, "TSNE2"], s=15, alpha=0.6, label=value)
    ax.set_xlabel("t-SNE 1"); ax.set_ylabel("t-SNE 2")
    ax.set_title("AlphaEarth t-SNE by Google Open Buildings 2.5D Confidence")
    ax.legend(bbox_to_anchor=(1.05, 1), loc="upper left")
    plt.tight_layout()
    path = output_dir / f"tsne_by_google25d_{slug}.png"
    fig.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {path}")


def join_results(original: gpd.GeoDataFrame, analysis: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    result_columns = [
        ID_FIELD, "isolation_forest_label", "isolation_forest_score",
        "isolation_forest_normalized", "isolation_forest_outlier", "TSNE1", "TSNE2",
    ]
    result_columns = [c for c in result_columns if c in analysis.columns]
    results = analysis[result_columns].copy()
    conflicts = [c for c in result_columns if c != ID_FIELD and c in original.columns]
    if conflicts:
        print("Replacing existing fields:", conflicts)
        original = original.drop(columns=conflicts)
    output = original.merge(results, on=ID_FIELD, how="left", validate="one_to_one")
    matched = output["isolation_forest_outlier"].notna().sum()
    print(f"Buildings with Isolation Forest results: {matched:,} / without: {len(output) - matched:,}")
    return output


def run(city, args) -> None:
    gee_gpkg = city.sources_dir / "gee" / f"{city.slug}_buildings_gee.gpkg"
    if not gee_gpkg.exists():
        raise FileNotFoundError(f"{gee_gpkg} not found. Run 0_get_gee_data.py first.")
    output_dir = city.sources_dir / "gee" / "outlier_results"
    output_gpkg = output_dir / f"{city.slug}_buildings_if_tsne.gpkg"
    if skip_existing(output_gpkg, args.force):
        return
    output_dir.mkdir(parents=True, exist_ok=True)

    original = gpd.read_file(gee_gpkg, layer=f"{city.slug}_buildings_gee")
    print(f"Buildings loaded: {len(original):,}")

    analysis, X, _ = prepare_alphaearth(original)
    X_scaled, labels, score, normalized, outlier = run_isolation_forest(X)
    analysis["isolation_forest_label"] = labels
    analysis["isolation_forest_score"] = score
    analysis["isolation_forest_normalized"] = normalized
    analysis["isolation_forest_outlier"] = outlier

    tsne_sample, tsne_X = sample_for_tsne(analysis, X_scaled, args.tsne_sample_size)
    tsne_sample = create_tsne(tsne_sample, tsne_X)
    plot_tsne_by_class(tsne_sample, output_dir, city.slug)
    plot_tsne_by_anomaly(tsne_sample, output_dir, city.slug)
    plot_tsne_by_google25d(tsne_sample, output_dir, city.slug)
    # TSNE1/TSNE2 land only on the sampled rows; everyone else gets NaN, same
    # treatment as buildings missing AlphaEarth values.
    analysis = analysis.merge(tsne_sample[[ID_FIELD, "TSNE1", "TSNE2"]], on=ID_FIELD, how="left")

    output = join_results(original, analysis)
    output.to_file(output_gpkg, layer=f"{city.slug}_buildings_if_tsne", driver="GPKG")
    output_csv = output_dir / f"{city.slug}_buildings_isolation_forest_tsne.csv"
    analysis.to_csv(output_csv, index=False)
    print(f"GeoPackage saved: {output_gpkg}")
    print(f"CSV saved: {output_csv}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True, help="Prepared city slug")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--tsne-sample-size", type=int, default=TSNE_MAX_SAMPLES,
        help="Max buildings to run t-SNE on, for the diagnostic plots only",
    )
    add_data_root_arg(parser)
    args = parser.parse_args()

    data_root = resolve_data_root(args.data_root)
    city = load_city(args.city, data_root)
    enable_logging(city.log_path)
    run(city, args)


if __name__ == "__main__":
    main()
