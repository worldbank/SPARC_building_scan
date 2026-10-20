# SPARC Building Scan

SPARC Building Scan is a geospatial workflow designed to generate high-quality, analysis-ready building footprints for urban areas.

## Repository Structure

- `src/`: Python source code to run building scan.
- `quarto_book/`: Markdown and Quarto source files for the documentation pages.
- `notebooks/`: Exploratory notebooks for analysis and experimentation.
- `.github/workflows/`: CI workflows to build preview of pages and publish.

## Running Building Scan

### Setup Python environment

This project requires **Python >=3.9**. It is recommended to use a virtual environment to manage dependencies. You can create and activate a virtual environment using the following commands:

```bash
python -m venv .venv # virtual env will use the default Python that runs this command; check with `python --version`
source .venv/bin/activate  # On Windows use `.venv\Scripts\activate`
```

Then install the required dependencies:

```bash
cd src
pip install -r requirements.txt
```

#### Using Anaconda

```bash
conda create -n sparc python=3.12
conda activate sparc
cd src
pip install -r requirements.txt
```

#### Using UV

uv is a fast Python package and environment manager. It downloads Python 3.12 automatically if it isn't already installed. Install uv with one of the following:

```bash
# macOS  and Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows:
winget install --id=astral-sh.uv -e

# pip (install outside of any conda environment)
pip install uv
```

Then create the environment and install dependencies:

   ```bash
   uv venv --python 3.12
   cd src
   uv pip install -r requirements.txt
   ```

> **Note:** If a conda environment is active (e.g. `(base)` in your prompt), run `conda deactivate` before using venv or uv. Otherwise the conda environment may be used instead of `.venv`.

### Running the workflow

The workflow is run as a series of 4 python scripts, which themselves are composed of smaller scripts that handle specific tasks (download, data prep, etc.). The only arguments that need to be passed are the city name, country, path for the city extent file (AOI), and data root (working directory where inputs and outputs are stored).

Activate the environment as above and run the scripts as follows:

If using windows:
```bash
cd src
.venv\Scripts\activate

python 1_data_integration/run_workflow.py ^
  --city-name "Juba" ^
  --country "South Sudan" ^
  --aoi C:/Users/WB514197/tmp/sparc/data/SPARC_AOI_Juba.geojson ^
  --data-root C:/Users/WB514197/tmp/sparc/workflow/ ^
  --skip-gba-height
```

> **Note:** gba-height currently raises issues on the WB network.

Alternatively, you can skip activation and run scripts with `uv run`.

### Contributing to documentation pages

The documentation is built with [Quarto](https://quarto.org/docs/get-started/). Installing it is optional, but helpful to preview changes locally. You can install it on WB machines via the software center. For other systems, follow the installation instructions on the Quarto website.

1. Clone the repository:

   ```bash
   git clone https://github.com/worldbank/SPARC_building_scan.git
   cd SPARC_building_scan
   ```

2. Create a new branch:

   ```bash
   git checkout -b my-new-branch
   ```

3. Edit the documentation pages inside `quarto_book/`.

4. Preview your changes locally (optional):
   ```bash
   quarto preview
   ```
   This opens a live preview in your browser that updates as you edit. To build the full site instead, run `quarto render`, which writes the output to `docs/`. This folder is not tracked by Git.

5. Commit your changes:

   ```bash
   git add .
   git commit -m "Description of changes"
   ```

6. Push your branch and open a pull request:

   ```bash
   git push -u origin my-new-branch
   ```
   Then go to the repository on GitHub and open a pull request from your branch into `main`.

## Github Pages CI Behavior

On every pull request, GitHub Actions will:

1. Install dependencies.
2. Render the Quarto project.
3. Upload the rendered files as an artifact.
4. Publish a preview to the `gh-pages` branch under `pr-preview/pr-<PR_NUMBER>/`.

The preview will be available at `https://worldbank.github.io/SPARC_building_scan/pr-preview/pr-<PR_NUMBER>/`.

New commits to an open pull request rerun the workflow and update the preview.

On every push to `main`, GitHub Actions will:

1. Install dependencies.
2. Render the Quarto project.
3. Upload the rendered files as an artifact.
4. Publish the updated documentation to the `gh-pages` branch.

The website is publicly available at https://worldbank.github.io/SPARC_building_scan/.