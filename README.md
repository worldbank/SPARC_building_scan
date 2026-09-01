# SPARC Building Scan

SPARC Building Scan is a geospatial analysis toolkit designed for Jupyter-based workflows with publishable Quarto outputs.

## Project Structure

- `src/`: Python package source code.
- `notebooks/`: Quarto notebooks (`.qmd`) for reproducible analysis.
- `docs/`: Rendered site output used for publication.
- `.github/workflows/`: CI workflows for build and publish.

## Quick Start

1. Create and activate a Python environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Render the Quarto notebook locally:

   ```bash
   quarto render
   ```

4. Open the rendered output in `docs/`.

## CI Behavior

On every pull request, GitHub Actions will:

1. Install dependencies.
2. Render the Quarto project.
3. Upload rendered files as an artifact.
4. Publish a PR preview to the `gh-pages` branch under:

   `pr-preview/pr-<PR_NUMBER>/`

To make previews publicly accessible, ensure GitHub Pages is enabled for the repository and points to the `gh-pages` branch.
