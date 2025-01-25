# My Podcasts

This codebase is a collection of scripts, CLIs, etc. that I use to:
1. Turn text into audio.
1. Produce a podcast RSS feed.
1. Host via [S3](https://www.thepolyglotdeveloper.com/2016/04/host-a-podcast-for-cheap-on-amazons-s3-service/).

## Alternatives

For $1/month, I could use [Mono](https://mono.fm/) to host podcasts. This covers 50GB of bandwidth used (~ 1500 downloads). All I need to do is bring the audio files.

## Install

### Environment variables

1. Copy `.envrc.example` to `.envrc`.
   ```
   cp .envrc.example .envrc
   ```

1. Fill out `.envrc`.

1. Source the environment variables defined in `.envrc`.
   ```
   direnv allow
   ```

### Python

1. Install or update `pyenv` (and `python-build` to get access to recent releases of python).
   ```
   brew update && brew install python-build pyenv
   brew update && brew upgrade python-build pyenv
   ```

1. Configure your shell to enable shims.
   ```
   # in e.g. ~/.bash_profile

   if which pyenv > /dev/null; then
     eval "$(pyenv init -)"
   fi
   ```

1. Install python.
   ```
   pyenv install $(cat .python-version)
   ```

### Poetry

1. Install Poetry [via its official installer](https://python-poetry.org/docs/#installing-with-the-official-installer).

### Python dependencies

1. Install dependencies.
   ```
   poetry install
   ```

1. Initialize the poetry virtual environment.
   ```
   poetry env activate
   ```

1. Install the pre-commit hooks.
   ```
   pre-commit install
   ```

## Development

* Initialize the poetry virtual environment.
  ```
  poetry env activate
  ```

* Run the auto formatter manually (although it will run automatically as a pre-commit hook).
  ```
  pre-commit run --all-files
  ```
