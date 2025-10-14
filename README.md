# My Podcasts

This repository is a collection of scripts and utilities that I use to produce audio files and generate podcast feeds. For example, you’ll find tools to process emails into text for text-to-speech conversion and to subsequently create podcast-ready artifacts.

## Overview

- **Email Processor:** Converts raw email content into cleaned text for TTS usage. Its new, data‑driven API is now encapsulated in a public class (`EmailProcessor`) so that you (and future downstream components, like an RSS feed builder) can consume a well‐structured dictionary.
- **TTS Joinery:** A text-to-speech helper that overcomes API limitations by chunking the content.

## Setup

For instructions on setting up your environment, installing Python (via pyenv), uv, and loading environment variables with direnv, please refer to the [Installation section](#installation) below.

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yourusername/my-podcasts.git
   cd my-podcasts
   ```

2. **Environment variables:**
   - Rename `.envrc.example` to `.envrc` and fill in your keys.
   - Allow direnv:
     ```bash
     direnv allow
     ```

3. **Python Setup:**
   - Use pyenv to install Python (see [pyenv installation](https://github.com/pyenv/pyenv#installation)).
   - The required Python version is specified in `.python-version`.

4. **Install Dependencies:**
   - Install uv if you haven't already:
     ```bash
     curl -LsSf https://astral.sh/uv/install.sh | sh
     ```
   - Then install project dependencies:
     ```bash
     uv sync
     ```

5. **Git Hooks (Optional):**
   - Set up a git pre-commit hook that runs ruff automatically:
     ```bash
     ./setup-git-hooks.sh
     ```
   - This will run `ruff check` and `ruff format --check` before each commit
   - To skip the hook for a specific commit: `git commit --no-verify`

6. **Code Quality:**
   - Run ruff for linting and formatting:
     ```bash
     # Check for linting issues
     uv run ruff check .

     # Auto-fix linting issues
     uv run ruff check --fix .

     # Format code
     uv run ruff format .

     # Run type checking
     uv run mypy .
     ```

7. **Running Tests:**
   - Execute the test suite using pytest:
     ```bash
     uv run pytest
     ```

## Development and CI

- **Local Testing:** Run tests with `uv run pytest`.
- **CI Pipeline:** On each push and pull request, the GitHub Actions workflow runs tests, style checks, and code coverage reports. See `.github/workflows/ci.yaml` for details.

For more module-specific details (e.g. using the email processor or TTS tools), please see the README files in the respective subdirectories.

---

Happy coding!
